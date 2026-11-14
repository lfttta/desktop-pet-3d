"""Proactive context: memory recall, tone, history persistence and decoupling.

Every provider is a local fake; no test reaches the network.
"""

from datetime import datetime, timezone
import random
import sqlite3
import sys
import time
import types

import pytest

from service.ai import proactive as cloud
from service.ai.provider import ProviderError
from service.behavior import proactive
from service.database import Database, SUBJECT_USER_ID
from service.memory import search as memory_search
from service.memory.store import MemoryStore
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


class FakeRuntime:
    def __init__(self, events=None):
        self.posted = []
        if events is not None:
            self.events = events

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

    def stream_chat(self, messages, key, on_delta=None, cancel_event=None):
        self.calls += 1
        self.messages.append(messages)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _policy_for(monkeypatch, state):
    package = types.ModuleType('service.desktop')
    package.__path__ = []
    module = types.ModuleType('service.desktop.monitor')

    def classify(snapshot, quiet_start='23:00', quiet_end='08:00',
                 idle_after_seconds=300, now=None, last_user_at='',
                 interaction_seconds=120):
        return {'state': state, 'reason': 'test'}

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


def _wait_for(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


def _scheduler(database, store, provider, monitor=None, interval=30.0,
               runtime=None, clock=None, started_at=None):
    def factory(ai_settings):
        return provider

    return proactive.ProactiveScheduler(
        runtime if runtime is not None else FakeRuntime(), store, database,
        monitor=monitor or FakeMonitor(), provider_factory=factory,
        clock=clock if clock is not None else (lambda: NOW.timestamp()),
        interval_seconds=interval, rng=random.Random(0),
        started_at=started_at)


def _run_cloud_once(database, settings, provider, **kwargs):
    scheduler = _scheduler(database, FakeStore(settings), provider, **kwargs)
    try:
        scheduler.tick(NOW)
        assert _wait_for(lambda: scheduler._results.qsize() == 1)
        scheduler.tick(NOW)
    finally:
        scheduler.close()
    return scheduler


def _seed_conversation(database, conversation_id='c-chat', title='聊天',
                       user_text='我最近在学吉他'):
    with database.transaction() as connection:
        connection.execute(
            'INSERT INTO conversations(id, title, subject_user_id) VALUES(?, ?, ?)',
            (conversation_id, title, SUBJECT_USER_ID))
        if user_text:
            connection.execute(
                'INSERT INTO messages(id, conversation_id, role, content, '
                'subject_user_id) VALUES(?, ?, ?, ?, ?)',
                ('m-user-' + conversation_id, conversation_id, 'user',
                 user_text, SUBJECT_USER_ID))


def _seed_memory(database, memory_id, content, importance=0.9):
    store = MemoryStore(database)
    with database.transaction() as connection:
        store.insert_memory(connection, {
            'id': memory_id,
            'content': content,
            'subject_user_id': SUBJECT_USER_ID,
            'category': 'general',
            'importance': importance,
            'status': 'active',
            'created_at': '2026-11-01T00:00:00Z',
            'updated_at': '2026-11-01T00:00:00Z',
            'last_reinforced_at': '2026-11-01T00:00:00Z',
            'mention_count': 1,
        })


# -- memory recall ---------------------------------------------------------


def test_recall_uses_search_for_context_with_three_entries_and_400_chars(
        database, monkeypatch):
    calls = []
    memory = '用户最近在学吉他，已经练了两周。'

    def fake_search_for_context(db, query, settings, limit, total_chars=None,
                                entry_chars=None, **kwargs):
        calls.append((db, query, settings, limit, total_chars, entry_chars))
        return [memory, {'snippet': '用户喜欢民谣。'}, None, '']

    monkeypatch.setattr(memory_search, 'search_for_context',
                        fake_search_for_context, raising=False)
    _seed_conversation(database)
    _policy_for(monkeypatch, 'active_interaction')
    settings = _settings()
    provider = ScriptedProvider(Completion('练琴记得歇一下。'))

    scheduler = _run_cloud_once(database, settings, provider)

    assert len(calls) == 1
    assert calls[0][0] is database
    assert calls[0][1] == '我最近在学吉他'
    # The scheduler hands over its SettingsStore so embedding retrieval can
    # reach the key provider; the cap and the total are separate values.
    assert getattr(calls[0][2], 'settings', calls[0][2]) is settings
    assert calls[0][3:] == (3, 1200, 400)
    system = provider.messages[0][0]['content']
    assert '可能记得的事' in system
    assert '用户最近在学吉他，已经练了两周。' in system
    assert '用户喜欢民谣。' in system
    assert scheduler.runtime.posted[0][2]['source'] == 'cloud'


def test_recall_uses_the_real_search_for_context_keyword_path(database,
                                                              monkeypatch):
    _seed_conversation(database)
    _seed_memory(database, 'mem-guitar', '用户最近在学吉他，已经练了两周。')
    _policy_for(monkeypatch, 'active_interaction')
    provider = ScriptedProvider(Completion('练琴记得歇一下。'))

    _run_cloud_once(database, _settings(), provider)

    system = provider.messages[0][0]['content']
    assert '可能记得的事' in system
    assert '用户最近在学吉他' in system


def test_recall_failure_yields_no_memory_but_the_message_is_still_sent(
        database, monkeypatch):
    def broken(db, query, settings, limit, total_chars=None, entry_chars=None,
               **kwargs):
        raise sqlite3.OperationalError('search unavailable')

    monkeypatch.setattr(memory_search, 'search_for_context', broken,
                        raising=False)
    _seed_conversation(database)
    _policy_for(monkeypatch, 'active_interaction')
    runtime = FakeRuntime(EventLog())
    provider = ScriptedProvider(Completion('今天也辛苦啦。'))

    scheduler = _scheduler(database, FakeStore(_settings()), provider,
                           runtime=runtime)
    try:
        scheduler.tick(NOW)
        assert _wait_for(lambda: scheduler._results.qsize() == 1)
        scheduler.tick(NOW)
    finally:
        scheduler.close()

    system = provider.messages[0][0]['content']
    assert '可能记得的事' not in system
    assert runtime.posted[0][2]['source'] == 'cloud'
    codes = [entry['code'] for entry in runtime.events.tail(50)]
    assert 'memory_failed' in codes


def test_recall_is_skipped_when_memory_is_explicitly_disabled(database,
                                                              monkeypatch):
    calls = []

    def fake_search_for_context(db, query, settings, limit, total_chars=None,
                                entry_chars=None, **kwargs):
        calls.append(query)
        return ['不该出现的记忆']

    monkeypatch.setattr(memory_search, 'search_for_context',
                        fake_search_for_context, raising=False)
    _seed_conversation(database)
    _policy_for(monkeypatch, 'active_interaction')
    settings = _settings()
    settings['memory'] = {'enabled': False}
    provider = ScriptedProvider(Completion('今天也辛苦啦。'))

    _run_cloud_once(database, settings, provider)

    assert calls == []
    assert '不该出现的记忆' not in provider.messages[0][0]['content']


# -- tone ------------------------------------------------------------------


def test_tone_directive_is_read_from_expression_tone(monkeypatch):
    module = types.ModuleType('service.persona_tone')
    module.TONE_DIRECTIVES = {'gentle': '口吻：温柔、克制，不催促。'}
    monkeypatch.setitem(sys.modules, 'service.persona_tone', module)
    settings = _settings()
    settings['expression']['tone'] = 'gentle'

    messages = cloud.build_messages(settings, 'godette', '', [])

    assert '口吻：温柔、克制，不催促。' in messages[0]['content']


def test_unknown_tone_and_missing_persona_tone_module_yield_no_directive(
        monkeypatch):
    monkeypatch.setitem(sys.modules, 'service.persona_tone', None)
    settings = _settings()
    settings['expression']['tone'] = 'gentle'
    messages = cloud.build_messages(settings, 'godette', '', [])
    assert 'gentle' not in messages[0]['content']
    assert '主动开口' in messages[0]['content']

    module = types.ModuleType('service.persona_tone')
    module.TONE_DIRECTIVES = {'gentle': '口吻：温柔。'}
    monkeypatch.setitem(sys.modules, 'service.persona_tone', module)
    settings['expression']['tone'] = 'unknown-tone'
    messages = cloud.build_messages(settings, 'godette', '', [])
    assert '口吻：温柔。' not in messages[0]['content']


# -- history persistence ---------------------------------------------------


def test_delivered_line_is_appended_to_the_latest_conversation(database,
                                                               monkeypatch):
    _seed_conversation(database)
    _policy_for(monkeypatch, 'active_interaction')
    provider = ScriptedProvider(Completion('今天也辛苦啦。'))

    scheduler = _run_cloud_once(database, _settings(), provider)

    assert scheduler.runtime.posted[0][2]['text'] == '今天也辛苦啦。'
    rows = database.connection.execute(
        "SELECT conversation_id, role, content FROM messages "
        "WHERE role='assistant'").fetchall()
    assert rows == [('c-chat', 'assistant', '今天也辛苦啦。')]
    assert database.connection.execute(
        'SELECT COUNT(*) FROM conversations').fetchone()[0] == 1


def test_history_creates_a_conversation_when_none_exists(database,
                                                          monkeypatch):
    _policy_for(monkeypatch, 'active_interaction')
    provider = ScriptedProvider(Completion('今天也辛苦啦。'))

    _run_cloud_once(database, _settings(), provider)

    conversations = database.connection.execute(
        'SELECT id, title FROM conversations').fetchall()
    assert len(conversations) == 1
    conversation_id, title = conversations[0]
    assert title == '今天也辛苦啦。'
    rows = database.connection.execute(
        "SELECT conversation_id, role, content FROM messages "
        "WHERE role='assistant'").fetchall()
    assert rows == [(conversation_id, 'assistant', '今天也辛苦啦。')]


def test_history_failure_only_records_an_event(database, monkeypatch):
    import service.features.proactive_store as proactive_store

    def broken_store(db, text):
        raise sqlite3.OperationalError('disk full')

    monkeypatch.setattr(proactive_store, 'append_assistant', broken_store)
    _policy_for(monkeypatch, 'active_interaction')
    runtime = FakeRuntime(EventLog())
    provider = ScriptedProvider(Completion('今天也辛苦啦。'))

    scheduler = _scheduler(database, FakeStore(_settings()), provider,
                           runtime=runtime)
    try:
        scheduler.tick(NOW)
        assert _wait_for(lambda: scheduler._results.qsize() == 1)
        scheduler.tick(NOW)
    finally:
        scheduler.close()

    assert runtime.posted[0][2]['text'] == '今天也辛苦啦。'
    assert database.connection.execute(
        'SELECT COUNT(*) FROM messages').fetchone()[0] == 0
    codes = [entry['code'] for entry in runtime.events.tail(50)]
    assert 'history_failed' in codes


# -- startup warmup --------------------------------------------------------


def test_cloud_is_not_attempted_during_the_startup_warmup(database,
                                                          monkeypatch):
    _policy_for(monkeypatch, 'active_interaction')
    runtime = FakeRuntime(EventLog())
    provider = ScriptedProvider(Completion('今天也辛苦啦。'))
    scheduler = _scheduler(database, FakeStore(_settings()), provider,
                           interval=1.0, runtime=runtime, clock=lambda: 0.0,
                           started_at=0.0)
    try:
        scheduler.tick(0.0)
        scheduler.tick(proactive.STARTUP_WARMUP_SECONDS - 1.0)
        assert provider.calls == 0
        assert runtime.posted == []

        scheduler.tick(proactive.STARTUP_WARMUP_SECONDS)
        assert _wait_for(lambda: scheduler._results.qsize() == 1)
        # Half a second later the interval has not elapsed: this tick only
        # drains the finished result and must not start a second call.
        scheduler.tick(proactive.STARTUP_WARMUP_SECONDS + 0.5)
    finally:
        scheduler.close()

    assert provider.calls == 1
    assert runtime.posted[0][2]['source'] == 'cloud'
    codes = [entry['code'] for entry in runtime.events.tail(50)]
    assert 'skipped_warmup' in codes


# -- decoupling local phrases from cloud messages --------------------------


def test_phrases_disabled_does_not_block_a_cloud_message(database,
                                                          monkeypatch):
    _policy_for(monkeypatch, 'active_interaction')
    settings = _settings()
    settings['expression']['phrases_enabled'] = False
    provider = ScriptedProvider(Completion('今天也辛苦啦。'))

    scheduler = _run_cloud_once(database, settings, provider)

    assert provider.calls == 1
    assert scheduler.runtime.posted[0][2]['source'] == 'cloud'
    assert scheduler.runtime.posted[0][2]['text'] == '今天也辛苦啦。'


def test_phrases_disabled_only_silences_the_local_fallback(database,
                                                            monkeypatch):
    _policy_for(monkeypatch, 'active_interaction')
    settings = _settings()
    settings['expression']['phrases_enabled'] = False
    provider = ScriptedProvider(ProviderError('timeout', 'slow'))

    scheduler = _scheduler(database, FakeStore(settings), provider)
    try:
        scheduler.tick(NOW)
        assert _wait_for(lambda: scheduler._results.qsize() == 1)
        scheduler.tick(NOW)
    finally:
        scheduler.close()

    assert provider.calls == 1
    assert scheduler.runtime.posted == []


def test_cloud_message_does_not_need_a_local_phrase_library(database,
                                                            monkeypatch):
    """An empty phrase library only affects the fallback, never the cloud path."""
    _policy_for(monkeypatch, 'active_interaction')
    monkeypatch.setattr(proactive.phrases, 'select_phrase',
                        lambda *args, **kwargs: None)
    provider = ScriptedProvider(Completion('今天也辛苦啦。'))

    scheduler = _run_cloud_once(database, _settings(), provider)

    assert scheduler.runtime.posted[0][2]['source'] == 'cloud'
    assert scheduler.runtime.posted[0][2]['text'] == '今天也辛苦啦。'
