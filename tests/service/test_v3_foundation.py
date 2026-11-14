"""V3 foundation contracts: migrations, capabilities and multi-frame transport."""

import io
import json
import sqlite3
import time

import pytest

from service import commands
from service.commands import dispatch
from service.database import SCHEMA_VERSION, Database
from service.protocol import ProtocolError
from service.runtime import serve
from service.settings import DEFAULTS, SCHEMA_VERSION as SETTINGS_SCHEMA_VERSION, SettingsStore


def _frame(command, payload=None, request_id='req-1'):
    return (json.dumps({'version': 1, 'id': request_id, 'type': command,
                        'payload': {} if payload is None else payload},
                       ensure_ascii=False) + '\n').encode('utf-8')


def test_v2_settings_migrate_to_v5_and_keep_old_fields(tmp_path):
    v2 = {
        'schema_version': 2, 'character_id': 'godette', 'persona_id': 'godette',
        'scale': 0.75, 'screen_id': '显示器 一', 'user_name': '小明',
        'placement': {'x': 0.2, 'y': 0.8, 'edge_x': -1, 'edge_y': 1},
    }
    path = tmp_path / 'settings.json'
    path.write_bytes((json.dumps(v2, ensure_ascii=False) + '\n').encode('utf-8'))
    store = SettingsStore(tmp_path)
    old_fields = {key: value for key, value in v2.items() if key != 'schema_version'}
    assert {key: store.settings[key] for key in old_fields} == old_fields
    assert store.settings['schema_version'] == SETTINGS_SCHEMA_VERSION
    for group in ('ai', 'memory', 'expression', 'autonomy', 'privacy', 'growth',
                  'desktop'):
        assert store.settings[group] == DEFAULTS[group]
    assert any(f'schema version {SETTINGS_SCHEMA_VERSION}' in warning
               for warning in store.warnings)
    assert json.loads(path.read_text('utf-8')) == store.settings
    assert SettingsStore(tmp_path).warnings == []


def test_v3_settings_reject_api_key_and_range_errors(tmp_path):
    from service.settings import ValidationError
    store = SettingsStore(tmp_path)
    before = store.settings
    for patch in ({'api_key': 'secret'}, {'ai': {'max_tokens': 0}},
                  {'ai': {'temperature': 3}}, {'autonomy': {'quiet_start': '25:00'}},
                  {'expression': {'style': {'warmth': 2}}}, {'ai': {'unknown': 1}}):
        with pytest.raises(ValidationError):
            store.update(patch)
    assert store.settings == before


def test_database_migrates_v1_to_v4_with_backup_and_data(tmp_path):
    path = tmp_path / 'pet.db'
    connection = sqlite3.connect(path)
    connection.execute('CREATE TABLE settings_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
    connection.execute('CREATE TABLE conversations ('
                       "id TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '',"
                       'created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)')
    connection.execute('CREATE TABLE messages ('
                       'id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL '
                       'REFERENCES conversations(id) ON DELETE CASCADE,'
                       "role TEXT NOT NULL CHECK(role IN ('user','assistant','system')) ,"
                       'content TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)')
    connection.execute('CREATE TABLE memories ('
                       'id TEXT PRIMARY KEY, content TEXT NOT NULL,'
                       'created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)')
    connection.execute("INSERT INTO conversations(id,title) VALUES ('c-1','旧会话')")
    connection.execute("INSERT INTO messages(id,conversation_id,role,content) "
                       "VALUES ('m-1','c-1','user','旧消息')")
    connection.execute("INSERT INTO memories(id,content) VALUES ('mem-1','旧记忆')")
    connection.execute('PRAGMA user_version=1')
    connection.commit()
    connection.close()
    original = path.read_bytes()

    db = Database(path)
    try:
        assert db.connection.execute('PRAGMA user_version').fetchone() == (SCHEMA_VERSION,)
        assert db.connection.execute('SELECT title FROM conversations').fetchone() == ('旧会话',)
        assert db.connection.execute('SELECT content FROM messages').fetchone() == ('旧消息',)
        columns = {row[1] for row in db.connection.execute('PRAGMA table_info(memories)')}
        assert {'subject_user_id', 'category', 'importance', 'source_type',
                'source_message_id', 'updated_at', 'deleted_at', 'supersedes_id',
                'fuzz_level', 'memory_level', 'topic_id', 'protected'} <= columns
        assert db.connection.execute(
            'SELECT subject_user_id, category FROM memories WHERE id=?', ('mem-1',)).fetchone() \
            == ('owner', 'general')
        tables = {row[0] for row in db.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert {'memories', 'memory_operations', 'memory_outbox', 'memory_jobs',
                'memory_conflicts', 'memory_embeddings', 'character_state',
                'growth_candidates', 'personality_history', 'usage_ledger',
                'phrase_usage', 'autonomy_state'} <= tables
        assert db.backup_path is not None and db.backup_path.is_file()
        assert db.capabilities['fts5'] is db.fts_enabled
    finally:
        db.close()
    backups = list(tmp_path.glob('pet.db.v1-backup-*'))
    assert len(backups) == 1 and backups[0].read_bytes() == original
    db = Database(path)
    try:
        assert db.backup_path is None
        assert db.connection.execute('PRAGMA user_version').fetchone() == (SCHEMA_VERSION,)
    finally:
        db.close()
    assert len(list(tmp_path.glob('pet.db.v1-backup-*'))) == 1


def test_missing_fts5_falls_back_to_like(tmp_path):
    db = Database(tmp_path / 'pet.db', enable_fts=False)
    try:
        assert db.fts_enabled is False
        assert db.capabilities == {'fts5': False}
        with db.transaction() as connection:
            connection.execute("INSERT INTO memories(id,content) VALUES ('m','喜欢下雨天')")
        found = db.connection.execute(
            "SELECT id FROM memories WHERE content LIKE '%下雨%'").fetchone()
        assert found == ('m',)
    finally:
        db.close()


def test_hello_advertises_v3_features_and_personas(tmp_path):
    store = SettingsStore(tmp_path)
    event, payload = dispatch({'type': 'hello', 'id': 'hello',
                               'payload': {'protocol_version': 1}}, store)
    assert event == 'ready'
    assert payload['settings']['schema_version'] == SETTINGS_SCHEMA_VERSION
    assert sorted(payload['features']) == sorted([
        'chat', 'memory', 'state', 'autonomy', 'expression', 'growth', 'timers',
        'usage', 'diag', 'desktop', 'voice'])
    expected = {
        'chat': ['chat.send', 'chat.cancel', 'chat.history'],
        'memory': ['memory.search', 'memory.propose', 'memory.correct',
                   'memory.delete', 'memory.list', 'memory.get', 'memory.history',
                   'memory.archive', 'memory.restore', 'memory.compact',
                   'memory.jobs.status', 'memory.conflicts.list',
                   'memory.conflicts.resolve', 'memory.index.rebuild',
                   'memory.index.status'],
        'state': ['state.get', 'state.reset'],
        'autonomy': ['autonomy.get', 'autonomy.update', 'autonomy.set_quiet'],
        'expression': ['expression.phrases.list', 'expression.phrases.set_enabled'],
        'growth': ['growth.list', 'growth.approve', 'growth.reject', 'growth.history',
                   'growth.rollback', 'personality.lock'],
        'timers': ['timer.set', 'timer.list', 'timer.cancel'],
        'usage': ['usage.get'],
        'diag': ['diag.export'],
        'desktop': ['desktop.status'],
    }
    for name, command_list in expected.items():
        assert payload['features'][name]['available'] is True
        assert payload['features'][name]['reason'] == ''
        assert payload['features'][name]['commands'] == command_list
    assert payload['personas'] == [{'id': 'godette', 'display_name': 'Godette'}]


class _SlowInput:
    def __init__(self, delay):
        self.delay = delay
        self.done = False

    def read1(self, size):
        if not self.done:
            self.done = True
            time.sleep(self.delay)
        return b''

    def read(self, size):
        return self.read1(size)


def test_tick_hook_and_outbound_queue_run_while_input_is_idle(tmp_path):
    from service.runtime import BoundedLog, Runtime
    output = io.BytesIO()
    runtime = Runtime(SettingsStore(tmp_path), output, BoundedLog(io.BytesIO()), tick_seconds=0.02)
    ticks = []

    def hook(now):
        ticks.append(now)
        runtime.post('tick', 'notice', {'count': len(ticks)})

    runtime.register_tick(hook)
    assert runtime.run(_SlowInput(0.15)) == 0
    assert len(ticks) >= 2
    notices = [json.loads(line) for line in output.getvalue().decode('utf-8').splitlines()]
    assert notices and all(item['id'] == 'tick' and item['type'] == 'notice' for item in notices)


def test_one_request_can_emit_delta_frames_then_done(tmp_path, monkeypatch):
    def streamed(request, store, ctx):
        ctx.emit('chat.delta', {'text': '第一段'})
        ctx.emit('chat.delta', {'text': '第二段'})
        return 'chat.done', {'message': {'role': 'assistant', 'content': '第一段第二段'},
                             'usage': {}, 'memory_ops': [], 'state_delta': {}}

    monkeypatch.setitem(commands.HANDLERS, 'chat.stream_test', streamed)
    output = io.BytesIO()
    errors = io.BytesIO()
    code = serve(tmp_path, io.BytesIO(_frame('chat.stream_test')), output, errors)
    assert code == 0
    replies = [json.loads(line) for line in output.getvalue().decode('utf-8').splitlines()]
    assert [(reply['id'], reply['type']) for reply in replies] == [
        ('req-1', 'chat.delta'), ('req-1', 'chat.delta'), ('req-1', 'chat.done')]
    assert [reply['payload'].get('text') for reply in replies[:2]] == ['第一段', '第二段']


@pytest.mark.parametrize('command', [
    'voice.synthesize', 'tts.cancel',
    'action.perform', 'personality.get', 'memory.fuzzy.search',
    'memory.index.rebuild', 'user.profile', 'memory.get', 'memory.update',
])
def test_reserved_commands_return_not_available(tmp_path, command):
    store = SettingsStore(tmp_path)
    with pytest.raises(ProtocolError) as caught:
        dispatch({'type': command, 'id': 'r', 'payload': {}}, store)
    assert caught.value.code == 'not_available'
    assert caught.value.request_id == 'r'


@pytest.mark.parametrize('command', ['desktop', 'desktop.notify', 'desktop.anything'])
def test_desktop_prefix_is_owned_by_the_desktop_feature(tmp_path, command):
    """``desktop`` is a real feature now, so only its own command is known."""
    store = SettingsStore(tmp_path)
    with pytest.raises(ProtocolError) as caught:
        dispatch({'type': command, 'id': 'd', 'payload': {}}, store)
    assert caught.value.code == 'unknown_command'
    assert caught.value.request_id == 'd'
    assert 'desktop' not in commands.RESERVED_PREFIXES


class _MemorySecretStore:
    def __init__(self):
        self.value = None

    def save(self, key):
        self.value = key

    def load(self):
        return self.value

    def clear(self):
        self.value = None
        return True


def test_ai_set_key_is_a_real_command_and_never_persists_the_key(tmp_path):
    store = SettingsStore(tmp_path)
    store.secrets_store = _MemorySecretStore()
    event, payload = dispatch({'type': 'ai.set_key', 'id': 'key',
                               'payload': {'key': 'sk-test-only'}}, store)
    assert (event, payload) == ('result', {'saved': True})
    assert store.secrets_store.value == 'sk-test-only'
    disk = (tmp_path / 'settings.json').read_text('utf-8')
    assert 'sk-test-only' not in disk
    assert 'key' not in store.settings
