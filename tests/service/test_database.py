import sqlite3

import pytest


def test_initialization_metadata_empty_tables_and_idempotence(tmp_path):
    from service.database import Database
    db = Database(tmp_path / 'pet.db')
    metadata = dict(db.connection.execute('SELECT key,value FROM settings_metadata'))
    assert metadata['settings_schema_version'] == '4'
    assert metadata['database_schema_version'] == '3'
    assert metadata['fts5_enabled'] in ('0', '1')
    for table in ('conversations', 'messages', 'memories', 'memory_jobs',
                  'memory_conflicts', 'memory_embeddings', 'personality_history'):
        assert db.connection.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] == 0
    db.close()
    db = Database(tmp_path / 'pet.db')
    assert db.connection.execute('PRAGMA user_version').fetchone() == (3,)
    db.close()


def test_transaction_rolls_back_all_changes_on_constraint_failure(tmp_path):
    from service.database import Database
    db = Database(tmp_path / 'pet.db')
    with pytest.raises(sqlite3.IntegrityError):
        with db.transaction() as connection:
            connection.execute("INSERT INTO conversations(id,title) VALUES ('c','test')")
            connection.execute("INSERT INTO messages(id,conversation_id,role,content) "
                               "VALUES ('m','missing','user','失败')")
    assert db.connection.execute('SELECT COUNT(*) FROM conversations').fetchone()[0] == 0
    with db.transaction() as connection:
        connection.execute("INSERT INTO conversations(id,title) VALUES ('c','中文')")
        connection.execute("INSERT INTO messages(id,conversation_id,role,content) "
                           "VALUES ('m','c','user','你好')")
    db.close()
    with sqlite3.connect(tmp_path / 'pet.db') as connection:
        assert connection.execute('SELECT content FROM messages').fetchone() == ('你好',)


def test_unsupported_database_version_is_not_overwritten(tmp_path):
    from service.database import Database
    path = tmp_path / 'pet.db'
    with sqlite3.connect(path) as connection:
        connection.execute('PRAGMA user_version=99')
    with pytest.raises(sqlite3.DatabaseError):
        Database(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute('PRAGMA user_version').fetchone() == (99,)


def _legacy_v2_database(path):
    """Create the phase-one v2 schema with a little data to migrate."""
    connection = sqlite3.connect(path)
    connection.execute('CREATE TABLE settings_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
    connection.execute('CREATE TABLE conversations ('
                       "id TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '',"
                       "subject_user_id TEXT NOT NULL DEFAULT 'owner',"
                       'created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)')
    connection.execute('CREATE TABLE messages ('
                       'id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL '
                       'REFERENCES conversations(id) ON DELETE CASCADE,'
                       "role TEXT NOT NULL CHECK(role IN ('user','assistant','system')),"
                       'content TEXT NOT NULL,'
                       "subject_user_id TEXT NOT NULL DEFAULT 'owner',"
                       'created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)')
    connection.execute('CREATE TABLE memories ('
                       'id TEXT PRIMARY KEY, content TEXT NOT NULL,'
                       'created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,'
                       "subject_user_id TEXT NOT NULL DEFAULT 'owner',"
                       "category TEXT NOT NULL DEFAULT 'general',"
                       'importance REAL NOT NULL DEFAULT 0.5,'
                       "source_type TEXT NOT NULL DEFAULT 'manual',"
                       'source_message_id TEXT,'
                       'updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,'
                       'deleted_at TEXT)')
    connection.execute('CREATE TABLE growth_candidates ('
                       'id TEXT PRIMARY KEY,'
                       "subject_user_id TEXT NOT NULL DEFAULT 'owner',"
                       "category TEXT NOT NULL DEFAULT 'general',"
                       'content TEXT NOT NULL,'
                       "status TEXT NOT NULL DEFAULT 'pending',"
                       'created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,'
                       'updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)')
    connection.execute("INSERT INTO conversations(id,title) VALUES ('c-1','旧会话')")
    connection.execute("INSERT INTO messages(id,conversation_id,role,content) "
                       "VALUES ('m-1','c-1','user','旧消息')")
    connection.execute("INSERT INTO memories(id,content) VALUES ('mem-1','旧记忆')")
    connection.execute("INSERT INTO growth_candidates(id,content) VALUES ('g-1','旧候选')")
    connection.execute('PRAGMA user_version=2')
    connection.commit()
    connection.close()


def test_v2_to_v3_migration_adds_columns_tables_and_backup(tmp_path):
    from service.database import Database
    path = tmp_path / 'pet.db'
    _legacy_v2_database(path)
    original = path.read_bytes()

    db = Database(path)
    try:
        assert db.connection.execute('PRAGMA user_version').fetchone() == (3,)
        metadata = dict(db.connection.execute('SELECT key,value FROM settings_metadata'))
        assert metadata['settings_schema_version'] == '4'
        assert metadata['database_schema_version'] == '3'
        assert db.connection.execute('SELECT title FROM conversations').fetchone() == ('旧会话',)
        assert db.connection.execute('SELECT content FROM messages').fetchone() == ('旧消息',)
        assert db.connection.execute('SELECT content FROM memories').fetchone() == ('旧记忆',)
        memory_columns = {row[1] for row in db.connection.execute('PRAGMA table_info(memories)')}
        assert {'supersedes_id', 'fuzz_level', 'memory_level', 'topic_id',
                'protected'} <= memory_columns
        growth_columns = {row[1] for row in db.connection.execute(
            'PRAGMA table_info(growth_candidates)')}
        assert {'trait', 'delta', 'evidence_ids', 'rule_version', 'applied_at',
                'decision'} <= growth_columns
        tables = {row[0] for row in db.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert {'memory_jobs', 'memory_conflicts', 'memory_embeddings',
                'personality_history'} <= tables
        assert db.backup_path is not None and db.backup_path.is_file()
        assert db.backup_path.read_bytes() == original
    finally:
        db.close()

    backups = list(tmp_path.glob('pet.db.v2-backup-*'))
    assert len(backups) == 1 and backups[0].read_bytes() == original
    db = Database(path)
    try:
        assert db.backup_path is None
        assert db.connection.execute('PRAGMA user_version').fetchone() == (3,)
    finally:
        db.close()
    assert len(list(tmp_path.glob('pet.db.v2-backup-*'))) == 1


def test_failed_migration_restores_pre_migration_database(tmp_path, monkeypatch):
    from service.database import Database
    path = tmp_path / 'pet.db'
    _legacy_v2_database(path)
    original = path.read_bytes()

    def fail(self, connection):
        raise sqlite3.OperationalError('injected migration failure')

    monkeypatch.setattr(Database, '_create_schema', fail)
    with pytest.raises(sqlite3.OperationalError):
        Database(path)
    assert path.read_bytes() == original
    with sqlite3.connect(path) as connection:
        assert connection.execute('PRAGMA user_version').fetchone() == (2,)
        assert connection.execute('SELECT content FROM memories').fetchone() == ('旧记忆',)
    assert len(list(tmp_path.glob('pet.db.v2-backup-*'))) == 1
