"""Proactive cloud message pipeline: prompt, validation and scheduler.

Every provider is a local fake; no test reaches the network.
"""

from datetime import datetime, timedelta, timezone
import json
import random
import sys
import threading
import time
import types

import pytest

from service.ai import proactive as cloud
from service.ai.provider import ProviderError
from service.behavior import proactive
from service.database import Database
from service.expression import phrases
from service.runtime import EventLog

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
FAKE_KEY = 'test-key-not-real'


# -- fixtures and fakes ----------------------------------------------------


@pytest.fixture
def database(tmp_path):
    db = Database(tmp_path / 'pet.db')
    try:
        yield db
    finally:
        db.close()


def _install_phrases(directory):
    """Replace the local phrase library with deterministic test entries."""
    disabled = [item['id'] for item in phrases.load_phrases()]
    path = directory / 'expression' / 'phrases.local.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        'version': 1,
        'disabled': disabled,
        'phrases': [
            {'id': 'test_care_a', 'text': '要不要歇一会儿？', 'category': 'care',
             'cooldown_seconds': 0, 'daily_limit': 20},
            {'id': 'test_care_b', 'text': '记得喝点水。', 'category': 'care',
             'cooldown_seconds': 0, 'daily_limit': 20},
            {'id': 'test_idle_a', 'text': '我在这儿呢。', 'category': 'idle',
             'cooldown_seconds': 0, 'daily_limit': 20},
        ],
    }, ensure_ascii=False), encoding='utf-8')


class FakeRuntime:
    def __init__(self):
        self.posted = []

    def post(self, request_id, event, payload):
        self.posted.append((request_id, event, payload))


class FakeStore:
    def __init__(self, settings):
        self.settings = settings
        self.ai_key_provider = lambda: FAKE_KEY
        self.ai_transport = None


class FakeMonitor:
    def __init__(self, state='active_interaction'):
        self.state = state
        self.calls = 0

    def snapshot(self):
        self.calls += 1
        return {
            'known': True,
            'fullscreen': self.state == 'fullscreen',
            'do_not_disturb': self.state == 'do_not_disturb',
            'locked': False,
            'idle_seconds': 0,
            'sampled_at': NOW.timestamp(),
        }


class Completion:
    def __init__(self, text, usage=None, model='fake-model'):
        self.text = text
        self.usage = usage if usage is not None else {
            'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15}
        self.model = model


class ScriptedProvider:
    def __init__(self, result=None):
        self.result = result if result is not None else Completion('今天也辛苦啦。')
        self.calls = 0
        self.messages = []
        self.keys = []

    def stream_chat(self, messages, key, on_delta=None, cancel_event=None):
        self.calls += 1
        self.messages.append(messages)
        self.keys.append(key)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class GatedProvider:
    """Blocks inside ``stream_chat`` until released or cancelled."""

    def __init__(self, text='先歇一会儿吧。', wait_seconds=3.0):
        self.text = text
        self.wait_seconds = wait_seconds
        self.release = threading.Event()
        self.started = threading.Event()
        self.calls = 0

    def stream_chat(self, messages, key, on_delta=None, cancel_event=None):
        self.calls += 1
        self.started.set()
        deadline = time.monotonic() + self.wait_seconds
        while time.monotonic() < deadline:
            if cancel_event is not None and cancel_event.is_set():
                raise ProviderError('cancelled', 'cancelled', False)
            if self.release.is_set():
                return Completion(self.text)
            time.sleep(0.005)
        return Completion(self.text)


def _policy_for(monkeypatch, state):
    """Install a fake desktop classifier.

    ``state`` may be a state string, a ``{'state': ...}`` holder (mutated by a
    test mid-flight) or a callable taking the snapshot.
    """
    package = types.ModuleType('service.desktop')
    package.__path__ = []
    module = types.ModuleType('service.desktop.monitor')

    def classify(snapshot, quiet_start='23:00', quiet_end='08:00',
                 idle_after_seconds=300, now=None, last_user_at='',
                 interaction_seconds=120):
        if callable(state):
            return {'state': state(snapshot), 'reason': 'test'}
        current = state['state'] if isinstance(state, dict) else state
        return {'state': current, 'reason': 'test'}

    module.classify = classify
    monkeypatch.setitem(sys.modules, 'service.desktop', package)
    monkeypatch.setitem(sys.modules, 'service.desktop.monitor', module)


def _settings(cloud_enabled=True, **autonomy):
    config = {
        'enabled': True,
        'quiet_start': '00:00',
        'quiet_end': '00:00',
        'min_seconds_between': 0,
        'max_per_hour': 5,
        'manual_quiet_until': '',
        'cloud_enabled': cloud_enabled,
    }
    config.update(autonomy)
    return {
        'persona_id': 'godette',
        'ai': {'currency': 'USD', 'input_price_per_million': 1.0,
               'output_price_per_million': 2.0},
        'expression': {'enabled': True, 'phrases_enabled': True},
        'autonomy': config,
        'desktop': {
            'enabled': True,
            'fullscreen_policy': 'silent',
            'dnd_policy': 'silent',
            'work_focus_policy': 'normal',
            'idle_policy': 'normal',
        },
    }


def _record_usage(database, tokens, cost, created_at=None):
    with database.transaction() as connection:
        connection.execute(
            'INSERT INTO usage_ledger(request_id, model, prompt_tokens, '
            'completion_tokens, total_tokens, cost, currency, created_at) '
            'VALUES(?,?,?,?,?,?,?,?)',
            ('seed', 'fake-model', tokens, 0, tokens, cost, 'USD',
             created_at or datetime.now(timezone.utc).isoformat()))


def _wait_for(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


def _scheduler(database, store, provider, monitor=None, interval=30.0,
               factory_calls=None):
    def factory(ai_settings):
        if factory_calls is not None:
            factory_calls.append(ai_settings)
        return provider

    return proactive.ProactiveScheduler(
        FakeRuntime(), store, database, monitor=monitor or FakeMonitor(),
        provider_factory=factory, clock=lambda: NOW.timestamp(),
        interval_seconds=interval, rng=random.Random(0))


# -- prompt and validation -------------------------------------------------


def test_build_messages_carries_persona_state_and_one_instruction():
    messages = cloud.build_messages(_settings(), 'godette', 'active_interaction',
                                    [{'role': 'user', 'content': '今天好累'},
                                     {'role': 'assistant', 'content': '抱抱'}])
    assert [item['role'] for item in messages] == ['system', 'user']
    system = messages[0]['content']
    assert '安全边界' in system
    assert '主动开口' in system
    assert '8 到 40' in system
    assert '不提问' in system
    user = messages[1]['content']
    assert 'active_interaction' in user
    assert '今天好累' in user and '抱抱' in user
    assert user.endswith('现在主动说一句。')
    # Only the prohibition names the observation channels; the user block
    # carries nothing about what the character might have observed.
    for token in ('屏幕', '窗口', '监控'):
        assert token not in user
    assert 'JSON' not in user

    minimal = cloud.build_messages(_settings(), 'godette', '', [])
    assert [item['role'] for item in minimal] == ['system', 'user']
    assert minimal[1]['content'] == '现在主动说一句。'


@pytest.mark.parametrize('text, expected', [
    ('  你好呀。  ', '你好呀。'),
    ('"你好呀。"', '你好呀。'),
    ('“你好呀。”', '你好呀。'),
    ('「你好呀。」', '你好呀。'),
    ('嗯\n\n我在，\t陪着你。', '嗯 我在， 陪着你。'),
])
def test_validate_text_cleans_valid_lines(text, expected):
    assert cloud.validate_text(text) == expected


@pytest.mark.parametrize('text', [
    '', '   ', None, 123, [], 'a' * 121,
    '我注意到你在写代码。',
    '检测到你在忙。',
    '屏幕那边好像很忙。',
    '窗口又切走了。',
    '我在监控你的进度。',
    '好的 JSON 马上来。',
    '重试一次 json 就好。',
    '你现在在 fullscreen 吧。',
    '你好像在 do_not_disturb 状态。',
    '你好像在 active_work 状态。',
])
def test_validate_text_rejects_empty_long_and_observational_lines(text):
    assert cloud.validate_text(text) is None


def test_validate_text_accepts_the_maximum_length():
    assert cloud.validate_text('好' * cloud.MAX_PROACTIVE_CHARS) == '好' * cloud.MAX_PROACTIVE_CHARS


def test_generate_normalizes_completion_and_swallows_provider_errors():
    provider = ScriptedProvider(Completion('你好呀。'))
    result = cloud.generate(provider, FAKE_KEY, [{'role': 'user', 'content': 'x'}])
    assert result['text'] == '你好呀。'
    assert result['usage']['total_tokens'] == 15
    assert result['model'] == 'fake-model'
    assert cloud.generate(ScriptedProvider(ProviderError('timeout', 'slow')),
                          FAKE_KEY, []) is None
    assert cloud.generate(ScriptedProvider(RuntimeError('boom')),
                          FAKE_KEY, []) is None


def test_recent_history_reads_the_latest_conversation(database):
    with database.transaction() as connection:
        connection.execute("INSERT INTO conversations(id, title) VALUES('c-old', 'old')")
        connection.execute("INSERT INTO conversations(id, title) VALUES('c-new', 'new')")
        connection.execute(
            'INSERT INTO messages(id, conversation_id, role, content) '
            "VALUES('m-1', 'c-old', 'user', '旧话题')")
        for index, (role, content) in enumerate(
                [('user', '第一句'), ('assistant', '第二句'),
                 ('user', '第三句'), ('assistant', '第四句')]):
            connection.execute(
                'INSERT INTO messages(id, conversation_id, role, content) '
                'VALUES(?,?,?,?)', ('n-%d' % index, 'c-new', role, content))
    history = cloud.recent_history(database, 2)
    assert history == [{'role': 'user', 'content': '第三句'},
                       {'role': 'assistant', 'content': '第四句'}]
    assert cloud.recent_history(database, 0) == []
    assert cloud.recent_history(None) == []


def test_recent_history_returns_empty_on_storage_failure():
    class Broken:
        connection = object()

    assert cloud.recent_history(Broken(), 2) == []


# -- scheduler -------------------------------------------------------------


def test_cloud_success_posts_and_records_both_ledgers(tmp_path, monkeypatch):
    database = Database(tmp_path / 'pet.db')
    try:
        _install_phrases(tmp_path)
        _policy_for(monkeypatch, 'active_interaction')
        provider = ScriptedProvider(Completion('今天也辛苦啦，慢慢来就好。'))
        monitor = FakeMonitor()
        scheduler = _scheduler(database, FakeStore(_settings()), provider,
                               monitor=monitor)
        try:
            scheduler.tick(NOW)
            assert _wait_for(lambda: scheduler._results.qsize() == 1)
            scheduler.tick(NOW)
        finally:
            scheduler.close()

        assert provider.calls == 1
        assert provider.keys == [FAKE_KEY]
        # One snapshot before starting, one policy re-check when draining.
        assert monitor.calls == 2
        assert len(scheduler.runtime.posted) == 1
        request_id, event, payload = scheduler.runtime.posted[0]
        assert (request_id, event) == ('autonomy', 'autonomy.bubble')
        assert payload == {
            'text': '今天也辛苦啦，慢慢来就好。',
            'phrase_id': '',
            'source': 'cloud',
            'created_at': NOW.astimezone(timezone.utc).isoformat(),
        }
        autonomy_row = database.connection.execute(
            'SELECT last_autonomous_at, hourly_count FROM autonomy_state').fetchone()
        assert autonomy_row[0]
        assert autonomy_row[1] == 1
        ledger = database.connection.execute(
            'SELECT model, prompt_tokens, completion_tokens FROM usage_ledger').fetchall()
        assert ledger == [('fake-model', 10, 5)]
    finally:
        database.close()


def test_provider_error_falls_back_to_a_local_phrase(tmp_path, monkeypatch):
    database = Database(tmp_path / 'pet.db')
    try:
        _install_phrases(tmp_path)
        _policy_for(monkeypatch, 'active_interaction')
        provider = ScriptedProvider(ProviderError('timeout', 'slow'))
        scheduler = _scheduler(database, FakeStore(_settings()), provider)
        try:
            scheduler.tick(NOW)
            assert _wait_for(lambda: scheduler._results.qsize() == 1)
            scheduler.tick(NOW)
        finally:
            scheduler.close()

        assert provider.calls == 1
        payload = scheduler.runtime.posted[0][2]
        assert payload['source'] == 'local'
        assert payload['phrase_id'] in ('test_care_a', 'test_care_b', 'test_idle_a')
        assert payload['text']
        assert database.connection.execute(
            'SELECT COUNT(*) FROM usage_ledger').fetchone()[0] == 0
        assert database.connection.execute(
            'SELECT hourly_count FROM autonomy_state').fetchone()[0] == 1
    finally:
        database.close()


@pytest.mark.parametrize('text', [
    '捏' * 130,
    '我注意到你又在加班了。',
    '这条按 JSON 返回。',
    '   ',
])
def test_invalid_cloud_text_falls_back_to_a_local_phrase(tmp_path, monkeypatch, text):
    database = Database(tmp_path / 'pet.db')
    try:
        _install_phrases(tmp_path)
        _policy_for(monkeypatch, 'active_interaction')
        provider = ScriptedProvider(Completion(text))
        scheduler = _scheduler(database, FakeStore(_settings()), provider)
        try:
            scheduler.tick(NOW)
            assert _wait_for(lambda: scheduler._results.qsize() == 1)
            scheduler.tick(NOW)
        finally:
            scheduler.close()
        payload = scheduler.runtime.posted[0][2]
        assert payload['source'] == 'local'
        assert payload['text'] != text
    finally:
        database.close()


def test_similar_repeat_falls_back_to_a_local_phrase(tmp_path, monkeypatch):
    database = Database(tmp_path / 'pet.db')
    try:
        _install_phrases(tmp_path)
        _policy_for(monkeypatch, 'active_interaction')
        repeated = '今天想和你说一句悄悄话。'
        provider = ScriptedProvider(Completion(repeated))
        scheduler = _scheduler(database, FakeStore(_settings()), provider)
        try:
            scheduler.tick(NOW)
            assert _wait_for(lambda: scheduler._results.qsize() == 1)
            scheduler.tick(NOW)
            first = scheduler.runtime.posted[0][2]
            assert first['source'] == 'cloud'

            scheduler.tick(NOW + timedelta(seconds=60))
            assert _wait_for(lambda: scheduler._results.qsize() == 1)
            scheduler.tick(NOW + timedelta(seconds=60))
        finally:
            scheduler.close()

        assert provider.calls == 2
        second = scheduler.runtime.posted[1][2]
        assert second['source'] == 'local'
        assert second['text'] != repeated
    finally:
        database.close()


def test_becoming_fullscreen_during_generation_drops_the_result(tmp_path, monkeypatch):
    database = Database(tmp_path / 'pet.db')
    try:
        _install_phrases(tmp_path)
        monitor = FakeMonitor('active_interaction')
        _policy_for(monkeypatch, lambda snapshot: monitor.state)
        provider = GatedProvider('先歇一会儿吧。')
        scheduler = _scheduler(database, FakeStore(_settings()), provider,
                               monitor=monitor)
        try:
            scheduler.tick(NOW)
            assert provider.started.wait(timeout=3.0)
            monitor.state = 'fullscreen'
            provider.release.set()
            assert _wait_for(lambda: scheduler._results.qsize() == 1)
            scheduler.tick(NOW)
        finally:
            scheduler.close()

        assert scheduler.runtime.posted == []
        assert database.connection.execute(
            'SELECT COUNT(*) FROM autonomy_state').fetchone()[0] == 0
        # The provider was already billed, so the usage must still be visible
        # even though the line itself was dropped.
        assert database.connection.execute(
            'SELECT COUNT(*) FROM usage_ledger').fetchone()[0] == 1
    finally:
        database.close()


def test_cloud_disabled_never_builds_a_provider(tmp_path, monkeypatch):
    database = Database(tmp_path / 'pet.db')
    try:
        _install_phrases(tmp_path)
        _policy_for(monkeypatch, 'active_interaction')
        provider = ScriptedProvider()
        factory_calls = []
        scheduler = _scheduler(database, FakeStore(_settings(cloud_enabled=False)),
                               provider, factory_calls=factory_calls)
        try:
            scheduler.tick(NOW)
            assert _wait_for(lambda: bool(scheduler.runtime.posted))
            scheduler.tick(NOW)
        finally:
            scheduler.close()
        assert provider.calls == 0
        assert factory_calls == []
        assert scheduler.runtime.posted[0][2]['source'] == 'local'
    finally:
        database.close()


def test_only_one_cloud_task_runs_at_a_time(tmp_path, monkeypatch):
    database = Database(tmp_path / 'pet.db')
    try:
        _install_phrases(tmp_path)
        _policy_for(monkeypatch, 'active_interaction')
        provider = GatedProvider()
        scheduler = _scheduler(database, FakeStore(_settings()), provider,
                               interval=0.0)
        try:
            scheduler.tick(NOW)
            assert provider.started.wait(timeout=3.0)
            scheduler.tick(NOW + timedelta(seconds=1))
            scheduler.tick(NOW + timedelta(seconds=2))
            assert provider.calls == 1
        finally:
            scheduler.close()
    finally:
        database.close()


def test_close_stops_pending_work_and_new_attempts(tmp_path, monkeypatch):
    database = Database(tmp_path / 'pet.db')
    try:
        _install_phrases(tmp_path)
        _policy_for(monkeypatch, 'active_interaction')
        provider = GatedProvider()
        scheduler = _scheduler(database, FakeStore(_settings()), provider,
                               interval=0.0)
        scheduler.tick(NOW)
        assert provider.started.wait(timeout=3.0)
        started = time.monotonic()
        scheduler.close()
        assert time.monotonic() - started < 1.5
        assert scheduler.runtime.posted == []
        scheduler.tick(NOW + timedelta(seconds=10))
        assert provider.calls == 1
        assert scheduler.runtime.posted == []
    finally:
        database.close()


def test_silent_policy_never_starts_a_task(tmp_path, monkeypatch):
    database = Database(tmp_path / 'pet.db')
    try:
        _install_phrases(tmp_path)
        _policy_for(monkeypatch, 'sleep_hours')
        provider = ScriptedProvider()
        scheduler = _scheduler(database, FakeStore(_settings()), provider)
        try:
            scheduler.tick(NOW)
            time.sleep(0.02)
            scheduler.tick(NOW + timedelta(seconds=60))
        finally:
            scheduler.close()
        assert provider.calls == 0
        assert scheduler.runtime.posted == []
    finally:
        database.close()


def test_autonomy_disabled_short_circuits(tmp_path, monkeypatch):
    database = Database(tmp_path / 'pet.db')
    try:
        _install_phrases(tmp_path)
        _policy_for(monkeypatch, 'active_interaction')
        provider = ScriptedProvider()
        settings = _settings(enabled=False)
        monitor = FakeMonitor()
        scheduler = _scheduler(database, FakeStore(settings), provider,
                               monitor=monitor)
        try:
            scheduler.tick(NOW)
            scheduler.tick(NOW + timedelta(seconds=60))
        finally:
            scheduler.close()
        assert provider.calls == 0
        assert monitor.calls == 0
        assert scheduler.runtime.posted == []
    finally:
        database.close()


def test_expression_phrases_disabled_short_circuits(tmp_path, monkeypatch):
    database = Database(tmp_path / 'pet.db')
    try:
        _install_phrases(tmp_path)
        _policy_for(monkeypatch, 'active_interaction')
        settings = _settings()
        settings['expression']['phrases_enabled'] = False
        provider = ScriptedProvider()
        scheduler = _scheduler(database, FakeStore(settings), provider)
        try:
            scheduler.tick(NOW)
        finally:
            scheduler.close()
        assert provider.calls == 0
        assert scheduler.runtime.posted == []
    finally:
        database.close()


def test_unknown_desktop_module_keeps_speaking_unrestricted(tmp_path, monkeypatch):
    # Fail the lazy import so the scheduler must fall back to state 'unknown'.
    monkeypatch.setitem(sys.modules, 'service.desktop.monitor', None)
    database = Database(tmp_path / 'pet.db')
    try:
        _install_phrases(tmp_path)
        settings = _settings()
        settings['desktop'] = {
            'enabled': True, 'work_focus_policy': 'silent', 'idle_policy': 'silent',
            'fullscreen_policy': 'silent', 'dnd_policy': 'silent',
        }
        provider = ScriptedProvider(Completion('今天也辛苦啦。'))
        scheduler = _scheduler(database, FakeStore(settings), provider)
        try:
            scheduler.tick(NOW)
            assert _wait_for(lambda: scheduler._results.qsize() == 1)
            scheduler.tick(NOW)
        finally:
            scheduler.close()
        assert scheduler.runtime.posted[0][2]['source'] == 'cloud'
    finally:
        database.close()


def test_budget_block_prevents_any_generation(tmp_path, monkeypatch):
    database = Database(tmp_path / 'pet.db')
    try:
        _install_phrases(tmp_path)
        _policy_for(monkeypatch, 'active_interaction')
        provider = ScriptedProvider()
        scheduler = _scheduler(database, FakeStore(_settings(max_per_hour=1)),
                               provider, interval=0.0)
        try:
            scheduler.tick(NOW)
            assert _wait_for(lambda: scheduler._results.qsize() == 1)
            scheduler.tick(NOW)
            assert provider.calls == 1
            scheduler.tick(NOW + timedelta(seconds=1))
        finally:
            scheduler.close()
        assert provider.calls == 1
        assert len(scheduler.runtime.posted) == 1
    finally:
        database.close()


def test_validate_and_policy_are_used_before_posting(tmp_path, monkeypatch):
    """The posted cloud text is the cleaned one, not the provider's raw line."""
    database = Database(tmp_path / 'pet.db')
    try:
        _install_phrases(tmp_path)
        _policy_for(monkeypatch, 'active_interaction')
        provider = ScriptedProvider(Completion('  "今天也辛苦啦。"  '))
        scheduler = _scheduler(database, FakeStore(_settings()), provider)
        try:
            scheduler.tick(NOW)
            assert _wait_for(lambda: scheduler._results.qsize() == 1)
            scheduler.tick(NOW)
        finally:
            scheduler.close()
        assert scheduler.runtime.posted[0][2]['text'] == '今天也辛苦啦。'
    finally:
        database.close()


# -- budget, privacy and cost guards ---------------------------------------


def test_daily_token_budget_blocks_the_paid_call(tmp_path, monkeypatch):
    database = Database(tmp_path / 'pet.db')
    try:
        _install_phrases(tmp_path)
        _policy_for(monkeypatch, 'active_interaction')
        settings = _settings()
        settings['ai'].update({'daily_token_budget': 1000,
                               'monthly_cost_budget': 0.0})
        _record_usage(database, 2000000, 0.0)
        provider = ScriptedProvider(Completion('今天也辛苦啦。'))
        scheduler = _scheduler(database, FakeStore(settings), provider)
        try:
            scheduler.tick(NOW)
            assert _wait_for(lambda: bool(scheduler.runtime.posted))
            scheduler.tick(NOW)
        finally:
            scheduler.close()
        # The chat path refuses here, so proactive generation must too.
        assert provider.calls == 0
        assert scheduler.runtime.posted[0][2]['source'] == 'local'
    finally:
        database.close()


def test_monthly_cost_budget_blocks_the_paid_call(tmp_path, monkeypatch):
    database = Database(tmp_path / 'pet.db')
    try:
        _install_phrases(tmp_path)
        _policy_for(monkeypatch, 'active_interaction')
        settings = _settings()
        settings['ai'].update({'daily_token_budget': 0,
                               'monthly_cost_budget': 15.0})
        _record_usage(database, 0, 999.0)
        provider = ScriptedProvider(Completion('今天也辛苦啦。'))
        scheduler = _scheduler(database, FakeStore(settings), provider)
        try:
            scheduler.tick(NOW)
            assert _wait_for(lambda: bool(scheduler.runtime.posted))
            scheduler.tick(NOW)
        finally:
            scheduler.close()
        assert provider.calls == 0
        assert scheduler.runtime.posted[0][2]['source'] == 'local'
    finally:
        database.close()


def test_disabled_desktop_is_neither_sampled_nor_put_in_the_prompt(
        tmp_path, monkeypatch):
    database = Database(tmp_path / 'pet.db')
    try:
        _install_phrases(tmp_path)
        _policy_for(monkeypatch, 'fullscreen')
        settings = _settings()
        settings['desktop']['enabled'] = False
        monitor = FakeMonitor('fullscreen')
        store = FakeStore(settings)
        store.desktop_monitor = monitor
        provider = ScriptedProvider(Completion('今天也辛苦啦。'))
        # Let the scheduler fall back to the monitor cached on the store.
        scheduler = _scheduler(database, store, provider)
        scheduler.monitor = None
        try:
            scheduler.tick(NOW)
            assert _wait_for(lambda: scheduler._results.qsize() == 1)
            scheduler.tick(NOW)
        finally:
            scheduler.close()
        assert monitor.calls == 0
        assert not hasattr(store, 'desktop_monitor')
        assert provider.calls == 1
        assert '当前状态' not in provider.messages[0][1]['content']
        assert scheduler.runtime.posted[0][2]['source'] == 'cloud'
    finally:
        database.close()


def test_cloud_disabled_during_generation_drops_the_result(tmp_path, monkeypatch):
    database = Database(tmp_path / 'pet.db')
    try:
        _install_phrases(tmp_path)
        _policy_for(monkeypatch, 'active_interaction')
        provider = GatedProvider('先歇一会儿吧。')
        store = FakeStore(_settings())
        scheduler = _scheduler(database, store, provider)
        try:
            scheduler.tick(NOW)
            assert provider.started.wait(timeout=3.0)
            store.settings = _settings(cloud_enabled=False)
            provider.release.set()
            assert _wait_for(lambda: scheduler._results.qsize() == 1)
            scheduler.tick(NOW)
        finally:
            scheduler.close()
        assert scheduler.runtime.posted
        assert scheduler.runtime.posted[0][2]['source'] == 'local'
        assert scheduler.runtime.posted[0][2]['text'] != '先歇一会儿吧。'
        # The call was paid for, so its usage must still be recorded.
        assert database.connection.execute(
            'SELECT COUNT(*) FROM usage_ledger').fetchone()[0] == 1
    finally:
        database.close()


def test_rejected_cloud_text_still_records_paid_usage(tmp_path, monkeypatch):
    database = Database(tmp_path / 'pet.db')
    try:
        _install_phrases(tmp_path)
        _policy_for(monkeypatch, 'active_interaction')
        provider = ScriptedProvider(Completion(
            '我注意到你在写代码。',
            {'prompt_tokens': 4000, 'completion_tokens': 1200,
             'total_tokens': 5200}))
        scheduler = _scheduler(database, FakeStore(_settings()), provider)
        try:
            scheduler.tick(NOW)
            assert _wait_for(lambda: scheduler._results.qsize() == 1)
            scheduler.tick(NOW)
        finally:
            scheduler.close()
        rows = database.connection.execute(
            'SELECT prompt_tokens, completion_tokens FROM usage_ledger').fetchall()
        assert rows == [(4000, 1200)]
        assert scheduler.runtime.posted[0][2]['source'] == 'local'
    finally:
        database.close()


def test_failed_generation_backs_off_instead_of_billing_every_interval(
        tmp_path, monkeypatch):
    database = Database(tmp_path / 'pet.db')
    try:
        _install_phrases(tmp_path)
        _policy_for(monkeypatch, 'active_interaction')
        provider = ScriptedProvider(ProviderError('timeout', 'slow'))
        scheduler = _scheduler(database, FakeStore(_settings()), provider,
                               interval=0.0)
        try:
            scheduler.tick(NOW)
            assert _wait_for(lambda: scheduler._results.qsize() == 1)
            scheduler.tick(NOW)
            assert provider.calls == 1
            # Inside the backoff window no new paid call is started.
            for seconds in (1, 5, 30):
                scheduler.tick(NOW + timedelta(seconds=seconds))
            assert provider.calls == 1
            # Once the backoff expires the scheduler may try again.
            scheduler.tick(NOW + timedelta(seconds=61))
            assert _wait_for(lambda: scheduler._results.qsize() == 1)
            scheduler.tick(NOW + timedelta(seconds=61))
            assert provider.calls == 2
        finally:
            scheduler.close()
    finally:
        database.close()


def test_proactive_events_reach_the_diagnostic_event_log(tmp_path, monkeypatch):
    database = Database(tmp_path / 'pet.db')
    try:
        _install_phrases(tmp_path)
        _policy_for(monkeypatch, 'active_interaction')
        provider = ScriptedProvider(Completion('今天也辛苦啦。'))
        runtime = FakeRuntime()
        runtime.events = EventLog()
        scheduler = proactive.ProactiveScheduler(
            runtime, FakeStore(_settings()), database, monitor=FakeMonitor(),
            provider_factory=lambda ai: provider,
            clock=lambda: NOW.timestamp(), interval_seconds=30.0,
            rng=random.Random(0))
        try:
            scheduler.tick(NOW)
            assert _wait_for(lambda: scheduler._results.qsize() == 1)
            scheduler.tick(NOW)
        finally:
            scheduler.close()
        codes = [entry['code'] for entry in runtime.events.tail(50)]
        assert 'posted' in codes
    finally:
        database.close()


def test_disabled_quiet_hours_do_not_force_sleep_hours(tmp_path):
    """The reported bug: the desktop classifier used its own 23:00-08:00 default.

    With quiet hours switched off (start == end) a 23:30 tick must still speak.
    This uses the real classifier, not the fake used by the policy tests.
    """
    database = Database(tmp_path / 'pet.db')
    try:
        _install_phrases(tmp_path)
        local_late = datetime.now().astimezone().replace(
            hour=23, minute=30, second=0, microsecond=0)
        provider = ScriptedProvider(Completion('今天也辛苦啦。'))
        scheduler = _scheduler(database, FakeStore(_settings()), provider)
        try:
            scheduler.tick(local_late)
            assert _wait_for(lambda: scheduler._results.qsize() == 1)
            scheduler.tick(local_late)
        finally:
            scheduler.close()
        assert provider.calls == 1
        assert scheduler.runtime.posted
        assert scheduler.runtime.posted[0][2]['source'] == 'cloud'
    finally:
        database.close()
