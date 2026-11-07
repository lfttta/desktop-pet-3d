"""Phase-two contracts: registered commands, capabilities, event log and migration."""

from concurrent.futures import ThreadPoolExecutor
import io
import sqlite3

import pytest

from service import commands
from service.commands import dispatch
from service.database import Database
from service.runtime import (BoundedLog, EVENT_LOG_MAXLEN, EventLog, RequestContext,
                             Runtime)
from service.settings import SettingsStore

PHASE2_COMMANDS = {
    'memory.get': {'memory_id': 'm-1'},
    'memory.history': {'memory_id': 'm-1'},
    'memory.archive': {'memory_id': 'm-1'},
    'memory.restore': {'memory_id': 'm-1'},
    'memory.compact': {},
    'memory.jobs.status': {},
    'memory.conflicts.list': {},
    'memory.conflicts.resolve': {'conflict_id': 'c-1', 'keep': 'a'},
    'memory.index.rebuild': {},
    'memory.index.status': {},
    'usage.get': {},
    'diag.export': {},
    'expression.phrases.list': {},
    'expression.phrases.set_enabled': {'phrase_id': 'p-1', 'enabled': False},
    'growth.list': {},
    'growth.approve': {'candidate_id': 'g-1'},
    'growth.reject': {'candidate_id': 'g-1'},
    'growth.history': {},
    'growth.rollback': {'history_id': 'h-1'},
    'personality.lock': {'trait': 'warmth', 'locked': True},
}


@pytest.mark.parametrize('command', PHASE2_COMMANDS)
def test_registered_phase2_commands_are_implemented(command):
    assert command in commands.HANDLERS


def test_hello_advertises_phase2_feature_groups(tmp_path):
    store = SettingsStore(tmp_path)
    _, payload = dispatch({'type': 'hello', 'id': 'hello',
                           'payload': {'protocol_version': 1}}, store)
    features = payload['features']
    assert set(features) == {'chat', 'memory', 'state', 'autonomy',
                             'expression', 'growth', 'usage', 'diag', 'desktop'}
    expected = {
        'expression': ['expression.phrases.list', 'expression.phrases.set_enabled'],
        'growth': ['growth.list', 'growth.approve', 'growth.reject', 'growth.history',
                   'growth.rollback', 'personality.lock'],
        'usage': ['usage.get'],
        'diag': ['diag.export'],
    }
    for name, commands_list in expected.items():
        assert features[name]['available'] is True
        assert features[name]['reason'] == ''
        assert features[name]['commands'] == commands_list
    memory_commands = features['memory']['commands']
    assert features['memory']['available'] is True
    for command in PHASE2_COMMANDS:
        if command.startswith('memory.'):
            assert command in memory_commands


def test_event_log_is_bounded_and_copies_tail_entries():
    log = EventLog()
    for index in range(EVENT_LOG_MAXLEN + 137):
        log.record('info', 'memory', f'code_{index}', 'event')
    assert len(log) == EVENT_LOG_MAXLEN
    entries = log.tail(EVENT_LOG_MAXLEN + 100)
    assert len(entries) == EVENT_LOG_MAXLEN
    assert set(entries[0]) == {'time', 'level', 'module', 'code', 'message'}
    assert entries[0]['code'] == 'code_137'
    assert entries[-1]['code'] == f'code_{EVENT_LOG_MAXLEN + 136}'
    assert log.tail(2)[-1]['code'] == entries[-1]['code']
    assert log.tail(0) == []
    assert log.tail(-1) == []
    entries[-1]['code'] = 'mutated'
    assert log.tail(1)[0]['code'] != 'mutated'


def test_event_log_is_thread_safe_under_concurrent_writes():
    log = EventLog()
    writers = 8
    per_writer = 300

    def worker(number):
        for index in range(per_writer):
            log.record('warning', 'thread', f'{number}-{index}', 'concurrent')

    with ThreadPoolExecutor(max_workers=writers) as pool:
        list(pool.map(worker, range(writers)))
    assert len(log) == EVENT_LOG_MAXLEN
    entries = log.tail(EVENT_LOG_MAXLEN)
    assert len(entries) == EVENT_LOG_MAXLEN
    assert all(entry['module'] == 'thread' for entry in entries)
    assert all(entry['code'] for entry in entries)


def test_event_log_truncates_long_and_multiline_messages():
    log = EventLog()
    log.record('INFO', 'module', 'code', 'line one\nline two\rline three')
    entry = log.tail(1)[0]
    assert '\n' not in entry['message'] and '\r' not in entry['message']
    assert entry['time'].endswith('Z')


def test_request_context_exposes_event_log_and_stderr_log_stays_separate(tmp_path):
    store = SettingsStore(tmp_path)
    errors = io.BytesIO()
    stderr_log = BoundedLog(errors)
    runtime = Runtime(store, io.BytesIO(), stderr_log)
    context = RequestContext(runtime, {'id': 'ctx', 'payload': {}})
    context.log.record('info', 'runtime', 'tick', 'heartbeat')
    context.log.record('error', 'memory', 'write_failed', 'body write failed')
    assert [entry['code'] for entry in context.log.tail(2)] == ['tick', 'write_failed']
    stderr_log.write('stderr still works')
    assert b'stderr still works' in errors.getvalue()
    assert context.log is not stderr_log


def test_database_migration_creates_a_backup(tmp_path):
    path = tmp_path / 'pet.db'
    connection = sqlite3.connect(path)
    connection.execute('CREATE TABLE conversations ('
                       "id TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '',"
                       'created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)')
    connection.execute('CREATE TABLE messages ('
                       'id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,'
                       "role TEXT NOT NULL, content TEXT NOT NULL,"
                       'created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)')
    connection.execute('CREATE TABLE memories ('
                       'id TEXT PRIMARY KEY, content TEXT NOT NULL,'
                       'created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)')
    connection.execute("INSERT INTO conversations(id,title) VALUES ('c','kept')")
    connection.execute('PRAGMA user_version=1')
    connection.commit()
    connection.close()
    original = path.read_bytes()

    db = Database(path)
    try:
        assert db.backup_path is not None and db.backup_path.is_file()
        assert db.backup_path.read_bytes() == original
        assert db.connection.execute('PRAGMA user_version').fetchone() == (3,)
        assert db.connection.execute('SELECT title FROM conversations').fetchone() == ('kept',)
    finally:
        db.close()
    backups = list(tmp_path.glob('pet.db.v1-backup-*'))
    assert len(backups) == 1 and backups[0].read_bytes() == original


def test_version_three_database_opens_without_new_backup(tmp_path):
    path = tmp_path / 'pet.db'
    db = Database(path)
    db.close()
    reopened = Database(path)
    try:
        assert reopened.backup_path is None
        assert reopened.connection.execute('PRAGMA user_version').fetchone() == (3,)
    finally:
        reopened.close()
    assert not list(tmp_path.glob('pet.db.v3-backup-*'))
