"""Reminder timers: schema, commands, local routing and due delivery.

The due check takes an explicit clock so the tests never sleep. Runtime tick
hooks use wall-clock UTC; only the hook wrapper receives the monotonic value
the runtime heartbeat passes.
"""

from datetime import datetime, timedelta, timezone
import sqlite3
import time

import pytest

from service.ai.orchestrator import ChatOrchestrator
from service.ai.router import Router
from service.commands import dispatch
from service.database import Database
from service.features import timers
from service.protocol import ProtocolError
from service.runtime import EventLog
from service.settings import SettingsStore

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)


class FakeContext:
    def __init__(self, database):
        self.database = database
        self.frames = []

    def emit(self, event, payload):
        self.frames.append((event, payload))

    def post(self, event, payload):
        self.frames.append((event, payload))

    def register_tick(self, hook):
        pass


class FakeRuntime:
    def __init__(self):
        self.frames = []
        self.hooks = []
        self.events = EventLog()

    def post(self, request_id, event, payload):
        self.frames.append((request_id, event, payload))

    def register_tick(self, hook):
        self.hooks.append(hook)


@pytest.fixture
def env(tmp_path):
    store = SettingsStore(tmp_path)
    database = Database(tmp_path / 'pet.db')
    context = FakeContext(database)
    try:
        yield store, database, context
    finally:
        database.close()


def _request(command, payload=None, request_id='req-timer'):
    return {'version': 1, 'id': request_id, 'type': command, 'payload': payload or {}}


def _insert_due(database, due_at, text='旧提醒'):
    with database.transaction() as connection:
        cursor = connection.execute(
            'INSERT INTO timers(due_at, text, created_at) VALUES(?,?,?)',
            (due_at, text, NOW.isoformat()))
        return cursor.lastrowid


# Schema and migration


def test_v5_schema_creates_timers_table(tmp_path):
    database = Database(tmp_path / 'pet.db')
    try:
        assert database.connection.execute('PRAGMA user_version').fetchone() == (5,)
        metadata = dict(database.connection.execute(
            'SELECT key,value FROM settings_metadata'))
        assert metadata['database_schema_version'] == '5'
        columns = [row[1] for row in database.connection.execute(
            'PRAGMA table_info(timers)')]
        assert columns == ['id', 'due_at', 'text', 'created_at', 'fired_at']
    finally:
        database.close()


def test_v4_database_gains_timers_table_and_backup(tmp_path):
    path = tmp_path / 'pet.db'
    database = Database(path)
    with database.transaction() as connection:
        connection.execute("INSERT INTO conversations(id,title) VALUES('c-v4','旧会话')")
    database.close()
    with sqlite3.connect(path) as connection:
        connection.execute('DROP TABLE timers')
        connection.execute('PRAGMA user_version=4')
    original = path.read_bytes()

    database = Database(path)
    try:
        assert database.connection.execute('PRAGMA user_version').fetchone() == (5,)
        assert database.connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
            "AND name='timers'").fetchone() == (1,)
        assert database.connection.execute(
            'SELECT title FROM conversations').fetchone() == ('旧会话',)
        assert database.backup_path is not None and database.backup_path.is_file()
        assert database.backup_path.read_bytes() == original
    finally:
        database.close()
    backups = list(tmp_path.glob('pet.db.v4-backup-*'))
    assert len(backups) == 1 and backups[0].read_bytes() == original
    database = Database(path)
    try:
        assert database.backup_path is None
    finally:
        database.close()


# Commands


def test_timer_set_stores_utc_due_at_and_lists_it(env):
    store, database, context = env
    before = datetime.now(timezone.utc)
    event, payload = dispatch(_request('timer.set', {'minutes': 5, 'text': '喝水'}),
                              store, context)
    after = datetime.now(timezone.utc)

    assert event == 'result'
    timer = payload['timer']
    due = datetime.fromisoformat(timer['due_at'])
    assert before + timedelta(minutes=5) <= due <= after + timedelta(minutes=5)
    assert timer['due_at'].endswith('+00:00')
    assert timer['text'] == '喝水'
    assert timer['fired_at'] is None

    _, listed = dispatch(_request('timer.list'), store, context)
    assert listed['count'] == 1
    assert listed['timers'][0]['id'] == timer['id']
    assert listed['timers'][0]['text'] == '喝水'


def test_timer_set_defaults_text_and_validates_input(env):
    store, database, context = env
    _, payload = dispatch(_request('timer.set', {'minutes': 1}), store, context)
    assert payload['timer']['text'] == timers.DEFAULT_TEXT

    for bad in ({}, {'minutes': 0}, {'minutes': -3}, {'minutes': '5'},
                {'minutes': True}, {'minutes': timers.MAX_MINUTES + 1}):
        with pytest.raises(ProtocolError) as caught:
            dispatch(_request('timer.set', bad), store, context)
        assert caught.value.code == 'invalid_request'

    with pytest.raises(ProtocolError) as caught:
        dispatch(_request('timer.set', {'minutes': 1, 'text': 3}), store, context)
    assert caught.value.code == 'invalid_request'


def test_timer_cancel_removes_one_or_all_pending(env):
    store, database, context = env
    first = timers.create(database, 5, '一', now=NOW)
    second = timers.create(database, 10, '二', now=NOW)

    _, payload = dispatch(_request('timer.cancel', {'id': first['id']}),
                          store, context)
    assert payload == {'cancelled': [first['id']], 'count': 1}
    assert [item['id'] for item in timers.pending(database)] == [second['id']]

    _, payload = dispatch(_request('timer.cancel'), store, context)
    assert payload['cancelled'] == [second['id']]
    assert timers.pending(database) == []

    with pytest.raises(ProtocolError) as caught:
        dispatch(_request('timer.cancel', {'id': 'many'}), store, context)
    assert caught.value.code == 'invalid_request'


def test_timer_commands_require_database():
    with pytest.raises(ProtocolError) as caught:
        dispatch(_request('timer.set', {'minutes': 1}), object())
    assert caught.value.code == 'storage_unavailable'


def test_cancelled_timer_id_can_be_reused_with_a_new_due_time(env):
    """DELETE frees the rowid, so the app cannot dedupe on the id alone.

    The ``timer.due`` payload carries ``due_at``; clients key on id+due_at so a
    reminder created after "取消提醒" still shows.
    """
    _, database, _ = env
    first = timers.create(database, 5, '旧提醒', now=NOW)
    assert timers.cancel(database, first['id']) == [first['id']]
    second = timers.create(database, 10, '新提醒', now=NOW)

    assert second['id'] == first['id']
    assert second['due_at'] != first['due_at']

    runtime = FakeRuntime()
    timers.tick(runtime, database, now=datetime.fromisoformat(second['due_at']))
    assert runtime.frames == [('timer', 'timer.due',
                               {'id': second['id'], 'due_at': second['due_at'],
                                'text': '新提醒'})]
    assert timers.check_due(database, now=datetime.fromisoformat(second['due_at'])) == []


# Due check and tick


def test_due_check_fires_once_at_the_injected_clock(env):
    _, database, _ = env
    timer = timers.create(database, 5, '喝水', now=NOW)

    assert timers.check_due(database, now=NOW + timedelta(minutes=4, seconds=59)) == []
    fired = timers.check_due(database, now=NOW + timedelta(minutes=5))
    assert fired == [{'id': timer['id'], 'due_at': timer['due_at'], 'text': '喝水'}]
    assert timers.check_due(database, now=NOW + timedelta(minutes=30)) == []
    row = database.connection.execute(
        'SELECT fired_at FROM timers WHERE id=?', (timer['id'],)).fetchone()
    assert row[0] is not None
    assert timers.pending(database) == []


def test_tick_posts_one_event_and_survives_failure(env, monkeypatch):
    _, database, _ = env
    timer_id = _insert_due(database, '2000-01-01T00:00:00+00:00')
    runtime = FakeRuntime()
    hook = timers.register_tick(runtime, database)
    assert hook in runtime.hooks

    hook(123.456)
    assert runtime.frames == [('timer', 'timer.due',
                               {'id': timer_id, 'due_at': '2000-01-01T00:00:00+00:00',
                                'text': '旧提醒'})]
    hook(124.0)
    assert len(runtime.frames) == 1

    def fail(*args, **kwargs):
        raise sqlite3.OperationalError('injected timer failure')

    monkeypatch.setattr(timers, 'check_due', fail)
    assert hook(125.0) is None
    assert runtime.events.tail(1)[0]['code'] == 'due_check_failed'
    assert len(runtime.frames) == 1


def test_pending_timers_survive_reopening_the_database(tmp_path):
    store = SettingsStore(tmp_path)
    database = Database(tmp_path / 'pet.db')
    context = FakeContext(database)
    _, payload = dispatch(_request('timer.set', {'minutes': 30, 'text': '重启后仍在'}),
                          store, context)
    timer_id = payload['timer']['id']
    database.close()

    database = Database(tmp_path / 'pet.db')
    try:
        _, listed = dispatch(_request('timer.list'), store, FakeContext(database))
        assert [item['id'] for item in listed['timers']] == [timer_id]
        assert listed['timers'][0]['text'] == '重启后仍在'
    finally:
        database.close()


# Local routing


def test_router_maps_reminder_phrases_to_timer_commands():
    router = Router()
    route = router.route('5分钟后提醒我')
    assert route.name == 'timer_set'
    assert route.action == 'timer.set'
    assert route.timer_minutes == 5
    assert route.timer_text is None

    route = router.route('5 分钟后提醒我 喝水')
    assert route.timer_minutes == 5
    assert route.timer_text == '喝水'

    route = router.route('取消提醒')
    assert route.name == 'timer_cancel'
    assert route.action == 'timer.cancel'

    assert router.route('提醒我') is None
    assert router.route('5分钟后再见') is None


# Chat end to end


def _chat(store, context, text):
    event, payload = dispatch(_request('chat.send', {'text': text}, 'chat-req'),
                              store, context)
    assert event == 'accepted'
    orchestrator = store._chat_orchestrator
    for _ in range(200):
        orchestrator.poll()
        if any(name == 'chat.done' for name, _payload in context.frames):
            break
        time.sleep(0.005)
    done = [payload for name, payload in context.frames if name == 'chat.done']
    assert done, context.frames
    return payload['conversation_id'], done[-1]


def test_chat_reminder_writes_a_timer_and_fires_it(env):
    store, database, context = env
    _, done = _chat(store, context, '5分钟后提醒我 喝水')

    assert done['message']['action'] == 'timer.set'
    assert done['timer_ops'] == [{'operation': 'timer.set', 'status': 'committed',
                                  'timer_id': done['timer_ops'][0]['timer_id'],
                                  'due_at': done['timer_ops'][0]['due_at'],
                                  'text': '喝水'}]
    pending = timers.pending(database)
    assert len(pending) == 1
    assert pending[0]['id'] == done['timer_ops'][0]['timer_id']
    assert pending[0]['text'] == '喝水'

    due_at = datetime.fromisoformat(pending[0]['due_at'])
    runtime = FakeRuntime()
    timers.tick(runtime, database, now=due_at)
    assert runtime.frames == [('timer', 'timer.due',
                               {'id': pending[0]['id'], 'due_at': pending[0]['due_at'],
                                'text': '喝水'})]
    assert timers.check_due(database, now=due_at + timedelta(minutes=1)) == []


def test_chat_timer_cancel_removes_pending_rows(env):
    store, database, context = env
    timers.create(database, 5, '喝水', now=NOW)
    _, done = _chat(store, context, '取消提醒')

    assert done['message']['action'] == 'timer.cancel'
    assert done['timer_ops'][0]['status'] == 'committed'
    assert timers.pending(database) == []


def test_chat_reminder_without_storage_reports_failure(tmp_path):
    store = SettingsStore(tmp_path)
    context = FakeContext(None)
    orchestrator = ChatOrchestrator(store, None)
    store._chat_orchestrator = orchestrator
    orchestrator.submit('r-1', '5分钟后提醒我 喝水', 'c-1', context)
    orchestrator.poll()
    done = [payload for name, payload in context.frames if name == 'chat.done'][-1]
    assert done['timer_ops'][0]['status'] == 'failed'
    assert '保存' in done['message']['content']
