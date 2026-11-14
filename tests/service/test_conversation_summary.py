"""Conversation summary storage, triggers and background behaviour."""

import json
import sqlite3
import threading
import time

import pytest

from service.ai.orchestrator import ChatOrchestrator
from service.ai.provider import Completion, ProviderError, TransportError
from service.ai import summary as summary_module
from service.ai.summary import (
    MAX_LAG_MESSAGES, MAX_SUMMARY_CHARS, MIN_MESSAGES, load_summary, maybe_summarize,
)
from service.commands import dispatch
from service.database import SCHEMA_VERSION, Database
from service.features import chat
from service.runtime import EventLog
from service.settings import SettingsStore

CONVERSATION = 'c-summary'
FAKE_KEY = 'test-key-not-real'


@pytest.fixture(autouse=True)
def _reset_retry_state():
    """Keep the module-level backoff from leaking between tests."""
    summary_module._RETRY_AT.clear()
    summary_module._FAILURES.clear()
    yield
    summary_module._RETRY_AT.clear()
    summary_module._FAILURES.clear()


# -- fixtures and fakes -----------------------------------------------------


def _seed(database, count, conversation_id=CONVERSATION):
    with database.transaction() as connection:
        connection.execute(
            'INSERT OR IGNORE INTO conversations(id, title, subject_user_id) '
            'VALUES(?, ?, ?)', (conversation_id, '测试会话', 'owner'))
        for index in range(count):
            role = 'user' if index % 2 == 0 else 'assistant'
            connection.execute(
                'INSERT INTO messages(id, conversation_id, role, content, subject_user_id) '
                'VALUES(?, ?, ?, ?, ?)',
                (conversation_id + '-m' + str(index), conversation_id, role,
                 '消息' + str(index), 'owner'))
    return database.connection.execute(
        'SELECT MAX(rowid) FROM messages WHERE conversation_id=?',
        (conversation_id,)).fetchone()[0]


def _latest_rowid(database, conversation_id=CONVERSATION):
    return database.connection.execute(
        'SELECT MAX(rowid) FROM messages WHERE conversation_id=?',
        (conversation_id,)).fetchone()[0]


def _store(database, log=None):
    class Store:
        pass

    store = Store()
    store.database = database
    store.ai_key_provider = lambda: FAKE_KEY
    if log is not None:
        store.log = log
    return store


class FakeProvider:
    def __init__(self, text='摘要内容', error=None, gate=None):
        self.text = text
        self.error = error
        self.gate = gate
        self.calls = 0
        self.messages = []
        self.keys = []

    def stream_chat(self, messages, key, on_delta=None, cancel_event=None):
        self.calls += 1
        self.messages.append(messages)
        self.keys.append(key)
        if self.gate is not None:
            self.gate.wait(5.0)
        if self.error is not None:
            raise self.error
        return Completion(self.text, {'prompt_tokens': 1, 'completion_tokens': 1,
                                      'total_tokens': 2}, 'fake-model')


def _wait_for(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def _downgrade_to_v3(path):
    with sqlite3.connect(path) as connection:
        connection.execute('DROP TABLE conversation_summaries')
        connection.execute('PRAGMA user_version=3')


# -- schema and migration ---------------------------------------------------


def test_schema_v4_creates_conversation_summaries_table(tmp_path):
    path = tmp_path / 'pet.db'
    database = Database(path)
    try:
        assert database.connection.execute('PRAGMA user_version').fetchone() == (SCHEMA_VERSION,)
        metadata = dict(database.connection.execute(
            'SELECT key, value FROM settings_metadata'))
        assert metadata['database_schema_version'] == str(SCHEMA_VERSION)
        columns = {row[1]: row[2] for row in database.connection.execute(
            'PRAGMA table_info(conversation_summaries)')}
        assert columns == {
            'conversation_id': 'TEXT',
            'summary': 'TEXT',
            'through_message_id': 'INTEGER',
            'updated_at': 'TEXT',
        }
        assert database.backup_path is None
    finally:
        database.close()
    database = Database(path)
    try:
        assert database.connection.execute('PRAGMA user_version').fetchone() == (SCHEMA_VERSION,)
        assert database.backup_path is None
    finally:
        database.close()


def test_v3_database_migrates_with_backup_and_keeps_rows(tmp_path):
    path = tmp_path / 'pet.db'
    database = Database(path)
    _seed(database, 2)
    database.close()
    _downgrade_to_v3(path)
    original = path.read_bytes()

    database = Database(path)
    try:
        assert database.connection.execute('PRAGMA user_version').fetchone() == (SCHEMA_VERSION,)
        assert database.connection.execute(
            'SELECT COUNT(*) FROM messages').fetchone() == (2,)
        assert database.connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
            "AND name='conversation_summaries'").fetchone() == (1,)
        assert database.backup_path is not None
        assert database.backup_path.read_bytes() == original
    finally:
        database.close()
    backups = list(tmp_path.glob('pet.db.v3-backup-*'))
    assert len(backups) == 1 and backups[0].read_bytes() == original


def test_failed_v4_migration_restores_the_v3_database(tmp_path, monkeypatch):
    path = tmp_path / 'pet.db'
    database = Database(path)
    _seed(database, 2)
    database.close()
    _downgrade_to_v3(path)
    original = path.read_bytes()

    def fail(self, connection):
        raise sqlite3.OperationalError('injected migration failure')

    monkeypatch.setattr(Database, '_create_schema', fail)
    with pytest.raises(sqlite3.OperationalError):
        Database(path)
    assert path.read_bytes() == original
    with sqlite3.connect(path) as connection:
        assert connection.execute('PRAGMA user_version').fetchone() == (3,)
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
            "AND name='conversation_summaries'").fetchone() == (0,)
        assert connection.execute('SELECT COUNT(*) FROM messages').fetchone() == (2,)
    backups = list(tmp_path.glob('pet.db.v3-backup-*'))
    assert len(backups) == 1 and backups[0].read_bytes() == original


def test_load_summary_returns_stored_text_and_defaults_to_empty(tmp_path):
    database = Database(tmp_path / 'pet.db')
    try:
        assert load_summary(database, CONVERSATION) == ''
        _seed(database, 1)
        with database.transaction() as connection:
            connection.execute(
                'INSERT INTO conversation_summaries'
                '(conversation_id, summary, through_message_id, updated_at) '
                'VALUES(?, ?, ?, ?)',
                (CONVERSATION, '用户喜欢下雨天', 1, '2026-01-01T00:00:00Z'))
        assert load_summary(database, CONVERSATION) == '用户喜欢下雨天'
        assert load_summary(None, CONVERSATION) == ''
        assert load_summary(database, None) == ''
        assert load_summary(database, 7) == ''
    finally:
        database.close()


# -- trigger conditions -----------------------------------------------------


def test_no_summary_below_the_message_minimum(tmp_path):
    database = Database(tmp_path / 'pet.db')
    provider = FakeProvider()
    try:
        _seed(database, MIN_MESSAGES)
        thread = maybe_summarize(_store(database), CONVERSATION, {},
                                 lambda ai: provider)
        assert thread is None
        assert provider.calls == 0
    finally:
        database.close()


def test_no_summary_when_the_existing_summary_is_less_than_the_lag(tmp_path):
    database = Database(tmp_path / 'pet.db')
    provider = FakeProvider()
    try:
        latest = _seed(database, 40)
        with database.transaction() as connection:
            connection.execute(
                'INSERT INTO conversation_summaries'
                '(conversation_id, summary, through_message_id, updated_at) '
                'VALUES(?, ?, ?, ?)',
                (CONVERSATION, '旧摘要', latest - MAX_LAG_MESSAGES, '2026-01-01T00:00:00Z'))
        thread = maybe_summarize(_store(database), CONVERSATION, {},
                                 lambda ai: provider)
        assert thread is None
        assert provider.calls == 0
    finally:
        database.close()


def test_summary_is_generated_when_the_lag_exceeds_the_limit(tmp_path):
    database = Database(tmp_path / 'pet.db')
    provider = FakeProvider(text='用户喜欢下雨天，周末想去看展。')
    try:
        latest = _seed(database, 30)
        with database.transaction() as connection:
            connection.execute(
                'INSERT INTO conversation_summaries'
                '(conversation_id, summary, through_message_id, updated_at) '
                'VALUES(?, ?, ?, ?)',
                (CONVERSATION, '旧摘要', latest - MAX_LAG_MESSAGES - 1,
                 '2026-01-01T00:00:00Z'))
        thread = maybe_summarize(_store(database), CONVERSATION, {},
                                 lambda ai: provider)
        assert thread is not None
        thread.join(3.0)
        assert not thread.is_alive()
        assert provider.calls == 1
        assert load_summary(database, CONVERSATION) == '用户喜欢下雨天，周末想去看展。'
        row = database.connection.execute(
            'SELECT through_message_id FROM conversation_summaries '
            'WHERE conversation_id=?', (CONVERSATION,)).fetchone()
        assert row == (latest,)
        rules = provider.messages[0][0]['content']
        assert '200' in rules
        assert '事实' in rules and '偏好' in rules and '未完成' in rules
        assert len(load_summary(database, CONVERSATION)) <= MAX_SUMMARY_CHARS
    finally:
        database.close()


def test_caller_does_not_wait_for_the_background_provider(tmp_path):
    database = Database(tmp_path / 'pet.db')
    gate = threading.Event()
    provider = FakeProvider(text='后台生成的摘要', gate=gate)
    try:
        _seed(database, MIN_MESSAGES + 1)
        started = time.perf_counter()
        thread = maybe_summarize(_store(database), CONVERSATION, {},
                                 lambda ai: provider)
        elapsed = time.perf_counter() - started
        assert thread is not None
        # The gated provider would hold the caller for up to five seconds.
        assert elapsed < 1.0
        assert thread.is_alive()
        gate.set()
        thread.join(3.0)
        assert not thread.is_alive()
        assert load_summary(database, CONVERSATION) == '后台生成的摘要'
    finally:
        gate.set()
        database.close()


def test_provider_failure_is_recorded_and_never_raised(tmp_path):
    database = Database(tmp_path / 'pet.db')
    log = EventLog()
    provider = FakeProvider(error=ProviderError('timeout', 'timed out', True))
    try:
        _seed(database, MIN_MESSAGES + 1)
        thread = maybe_summarize(_store(database, log), CONVERSATION, {},
                                 lambda ai: provider)
        assert thread is not None
        thread.join(3.0)
        assert not thread.is_alive()
        assert load_summary(database, CONVERSATION) == ''
        codes = [entry['code'] for entry in log.tail()]
        assert codes == ['summary_failed']
        assert log.tail()[0]['module'] == 'summary'
    finally:
        database.close()


def test_summary_is_skipped_when_the_budget_is_exhausted(tmp_path):
    database = Database(tmp_path / 'pet.db')
    log = EventLog()
    provider = FakeProvider()
    try:
        _seed(database, MIN_MESSAGES + 1)
        with database.transaction() as connection:
            connection.execute(
                'INSERT INTO usage_ledger(request_id, model, prompt_tokens, '
                'completion_tokens, total_tokens, cost) '
                "VALUES('seed', 'fake-model', 20, 0, 20, 0)")
        settings = {'ai': {'daily_token_budget': 10, 'max_tokens': 5}}
        thread = maybe_summarize(_store(database, log), CONVERSATION, settings,
                                 lambda ai: provider)
        assert thread is None
        assert provider.calls == 0
        assert load_summary(database, CONVERSATION) == ''
        codes = [entry['code'] for entry in log.tail()]
        assert 'summary_skipped' in codes
    finally:
        database.close()


def test_empty_reply_does_not_retry_on_the_next_turn(tmp_path):
    database = Database(tmp_path / 'pet.db')
    provider = FakeProvider(text='')
    try:
        _seed(database, MIN_MESSAGES + 1)
        thread = maybe_summarize(_store(database), CONVERSATION, {},
                                 lambda ai: provider)
        assert thread is not None
        thread.join(3.0)
        assert not thread.is_alive()
        assert provider.calls == 1
        assert load_summary(database, CONVERSATION) == ''
        # The paid call was still billed even though the reply was unusable.
        usage = database.connection.execute(
            'SELECT request_id FROM usage_ledger WHERE request_id=?',
            ('summary',)).fetchall()
        assert usage == [('summary',)]
        # The failure backoff suppresses the immediate next attempt.
        assert maybe_summarize(_store(database), CONVERSATION, {},
                               lambda ai: provider) is None
        assert provider.calls == 1
    finally:
        database.close()


# -- chat integration -------------------------------------------------------


def _sse(payload):
    return ('data: ' + json.dumps(payload, ensure_ascii=False) + '\n').encode('utf-8')


class FakeResponse:
    def __init__(self, lines=(), gate=None):
        self._lines = list(lines)
        self.headers = {'Content-Type': 'text/event-stream'}
        self.gate = gate

    def readline(self, size=-1):
        if self.gate is not None:
            self.gate.wait(5.0)
        if not self._lines:
            return b''
        return self._lines.pop(0)

    def close(self):
        pass


def _stream_response(text, gate=None):
    payload = json.dumps({
        'text': text,
        'speak': text,
        'action': 'none',
        'memory_candidates': [],
        'state_suggestion': {},
        'confidence': 0.9,
    }, ensure_ascii=False)
    pieces = [payload[index:index + 7] for index in range(0, len(payload), 7)]
    lines = [_sse({'model': 'fake-model',
                   'choices': [{'delta': {'content': piece}}]}) for piece in pieces]
    lines.append(b'data: [DONE]\n')
    return FakeResponse(lines, gate=gate)


def _text_response(text, gate=None):
    """Stream plain summary text, which is what the summary prompt asks for."""
    pieces = [text[index:index + 2] for index in range(0, len(text), 2)]
    lines = [_sse({'model': 'fake-model',
                   'choices': [{'delta': {'content': piece}}]}) for piece in pieces]
    lines.append(b'data: [DONE]\n')
    return FakeResponse(lines, gate=gate)


class ScriptedTransport:
    def __init__(self, scripts):
        self.scripts = list(scripts)
        self.calls = 0
        self.requests = []

    def send(self, url, headers, body, connect_timeout, read_timeout):
        self.requests.append(json.loads(body.decode('utf-8')))
        item = self.scripts[min(self.calls, len(self.scripts) - 1)]
        self.calls += 1
        if isinstance(item, Exception):
            raise item
        return item


class FakeContext:
    def __init__(self, database):
        self.database = database
        self.events = []

    def emit(self, event, payload):
        self.events.append((event, payload))

    def post(self, event, payload):
        self.events.append((event, payload))

    def register_tick(self, hook):
        pass

    def terminal(self):
        for event, payload in reversed(self.events):
            if event in ('chat.done', 'chat.error'):
                return event, payload
        return None


def _chat_env(tmp_path, transport):
    store = SettingsStore(tmp_path)
    store.ai_key_provider = lambda: FAKE_KEY
    store.ai_transport = transport
    database = Database(tmp_path / 'pet.db')
    orchestrator = ChatOrchestrator(store, database, transport=transport,
                                    retry_delay=0)
    store._chat_orchestrator = orchestrator
    return store, database, orchestrator, FakeContext(database)


def _pump(orchestrator, ctx, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        orchestrator.poll()
        terminal = ctx.terminal()
        if terminal is not None:
            return terminal
        time.sleep(0.005)
    raise AssertionError('no terminal chat event: %r' % (ctx.events,))


def _send(store, ctx, text='讲个笑话吧', request_id='req-1', conversation_id=''):
    payload = {'text': text}
    if conversation_id:
        payload['conversation_id'] = conversation_id
    return dispatch({'type': 'chat.send', 'id': request_id,
                     'payload': payload}, store, ctx)


def test_chat_send_triggers_summary_after_assistant_is_persisted(tmp_path, monkeypatch):
    transport = ScriptedTransport([_stream_response('好的呀')])
    store, database, orchestrator, ctx = _chat_env(tmp_path, transport)
    seen = {}

    def fake_maybe(store_arg, conversation_id, settings, provider_factory):
        seen['assistant'] = database.connection.execute(
            "SELECT content FROM messages WHERE role='assistant'").fetchall()
        seen['conversation_id'] = conversation_id
        seen['provider_factory'] = provider_factory
        return None

    monkeypatch.setattr(chat, 'maybe_summarize', fake_maybe)
    try:
        event, payload = _send(store, ctx)
        assert event == 'accepted'
        terminal, done = _pump(orchestrator, ctx)
        assert terminal == 'chat.done'
        assert done['message']['content'] == '好的呀'
        assert seen['assistant'] == [('好的呀',)]
        assert seen['conversation_id'] == payload['conversation_id']
        assert callable(seen['provider_factory'])
        assert transport.calls == 1
    finally:
        database.close()


def test_chat_reply_is_not_delayed_by_a_slow_summary_provider(tmp_path):
    gate = threading.Event()
    transport = ScriptedTransport([_stream_response('好的呀'),
                                   _text_response('后台摘要', gate=gate)])
    store, database, orchestrator, ctx = _chat_env(tmp_path, transport)
    try:
        _seed(database, MIN_MESSAGES)
        event, payload = _send(store, ctx, conversation_id=CONVERSATION)
        assert event == 'accepted'
        # The summary request is gated; the chat terminal event must still
        # arrive without releasing it.
        terminal, done = _pump(orchestrator, ctx, timeout=3.0)
        assert terminal == 'chat.done'
        assert done['message']['content'] == '好的呀'
        assert not gate.is_set()
        assert load_summary(database, payload['conversation_id']) == ''
        gate.set()
        assert _wait_for(
            lambda: load_summary(database, payload['conversation_id']) == '后台摘要')
    finally:
        gate.set()
        database.close()


def test_chat_works_when_summary_generation_fails(tmp_path):
    transport = ScriptedTransport([
        _stream_response('好的呀'),
        TransportError('connection', 'summary endpoint down'),
    ])
    store, database, orchestrator, ctx = _chat_env(tmp_path, transport)
    store.log = EventLog()
    try:
        _seed(database, MIN_MESSAGES)
        event, payload = _send(store, ctx, conversation_id=CONVERSATION)
        assert event == 'accepted'
        terminal, done = _pump(orchestrator, ctx, timeout=3.0)
        assert terminal == 'chat.done'
        assert done['message']['content'] == '好的呀'
        assert load_summary(database, payload['conversation_id']) == ''
        assert _wait_for(
            lambda: any(entry['code'] == 'summary_failed'
                        for entry in store.log.tail()))
    finally:
        database.close()


def test_chat_survives_an_exception_from_the_summary_hook(tmp_path, monkeypatch):
    transport = ScriptedTransport([_stream_response('好的呀')])
    store, database, orchestrator, ctx = _chat_env(tmp_path, transport)

    def broken(*args, **kwargs):
        raise RuntimeError('summary hook exploded')

    monkeypatch.setattr(chat, 'maybe_summarize', broken)
    try:
        event, payload = _send(store, ctx)
        assert event == 'accepted'
        terminal, done = _pump(orchestrator, ctx)
        assert terminal == 'chat.done'
        assert done['message']['content'] == '好的呀'
        assert database.connection.execute(
            "SELECT content FROM messages WHERE role='assistant'").fetchall() == [
                ('好的呀',)]
    finally:
        database.close()


def test_degraded_reply_does_not_trigger_a_summary(tmp_path, monkeypatch):
    transport = ScriptedTransport([
        _text_response('not json'), _text_response('still not json')])
    store, database, orchestrator, ctx = _chat_env(tmp_path, transport)
    calls = []
    monkeypatch.setattr(chat, 'maybe_summarize',
                        lambda *args, **kwargs: calls.append(args))
    try:
        event, payload = _send(store, ctx)
        assert event == 'accepted'
        terminal, done = _pump(orchestrator, ctx)
        assert terminal == 'chat.done'
        assert done['usage']['degraded'] is True
        assert calls == []
    finally:
        database.close()


def test_local_only_reply_does_not_trigger_a_summary(tmp_path, monkeypatch):
    transport = ScriptedTransport([_stream_response('不会用到')])
    store, database, orchestrator, ctx = _chat_env(tmp_path, transport)
    store.update({'ai': {'daily_token_budget': 1, 'max_tokens': 1}})
    with database.transaction() as connection:
        connection.execute(
            'INSERT INTO usage_ledger(request_id, model, prompt_tokens, '
            'completion_tokens, total_tokens, cost) '
            "VALUES('seed', 'fake-model', 10, 0, 10, 0)")
    calls = []
    monkeypatch.setattr(chat, 'maybe_summarize',
                        lambda *args, **kwargs: calls.append(args))
    try:
        event, payload = _send(store, ctx)
        assert event == 'accepted'
        terminal, done = _pump(orchestrator, ctx)
        assert terminal == 'chat.done'
        assert done['usage']['local_only'] is True
        assert transport.calls == 0
        assert calls == []
    finally:
        database.close()
