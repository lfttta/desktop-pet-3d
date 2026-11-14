"""Single-connection schema initialization, migrations and explicit transactions."""

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import shutil
import sqlite3
import uuid

SCHEMA_VERSION = 5
SETTINGS_SCHEMA_VERSION = 4
SUBJECT_USER_ID = 'owner'

TABLES = (
    'conversations', 'messages', 'conversation_summaries', 'memories',
    'memory_operations', 'memory_outbox',
    'memory_jobs', 'memory_conflicts', 'memory_embeddings',
    'character_state', 'growth_candidates', 'personality_history',
    'usage_ledger', 'phrase_usage', 'autonomy_state', 'timers',
)

_MEMORY_ADDITIONS = (
    ('subject_user_id', "TEXT NOT NULL DEFAULT 'owner'"),
    ('category', "TEXT NOT NULL DEFAULT 'general'"),
    ('importance', 'REAL NOT NULL DEFAULT 0.5'),
    ('source_type', "TEXT NOT NULL DEFAULT 'manual'"),
    ('source_message_id', 'TEXT'),
    ('updated_at', "TEXT NOT NULL DEFAULT ''"),
    ('deleted_at', 'TEXT'),
    ('supersedes_id', 'TEXT'),
    ('fuzz_level', 'INTEGER DEFAULT 0'),
    ('memory_level', "TEXT DEFAULT 'short'"),
    ('topic_id', 'TEXT'),
    ('protected', 'INTEGER DEFAULT 0'),
)

_GROWTH_ADDITIONS = (
    ('trait', "TEXT DEFAULT ''"),
    ('delta', 'REAL DEFAULT 0'),
    ('evidence_ids', "TEXT DEFAULT '[]'"),
    ('rule_version', 'INTEGER DEFAULT 1'),
    ('applied_at', 'TEXT'),
    ('decision', "TEXT DEFAULT ''"),
)


class Database:
    def __init__(self, path, enable_fts=True):
        self.path = Path(path)
        self.backup_path = None
        self.fts_enabled = False
        self.capabilities = {'fts5': False}
        self.connection = sqlite3.connect(self.path, timeout=1.0)
        try:
            self.connection.execute('PRAGMA foreign_keys=ON')
            self.connection.execute('PRAGMA synchronous=FULL')
            version = self.connection.execute('PRAGMA user_version').fetchone()[0]
            if version not in (0, 1, 2, 3, 4, 5):
                raise sqlite3.DatabaseError('unsupported database schema')
            if self._needs_backup(version):
                self.backup_path = self._backup(version)
            with self.transaction() as connection:
                self._create_schema(connection)
                self.fts_enabled = self._setup_fts(connection) if enable_fts else False
                self._set_metadata(connection)
                connection.execute(f'PRAGMA user_version={SCHEMA_VERSION}')
            self.capabilities = {'fts5': self.fts_enabled}
        except BaseException:
            self.connection.close()
            if self.backup_path is not None and self.backup_path.is_file():
                # A failed migration must leave the pre-migration database intact.
                try:
                    shutil.copyfile(self.backup_path, self.path)
                except OSError:
                    pass
            raise

    def _needs_backup(self, version):
        if version in (1, 2, 3, 4):
            return True
        if version != 0:
            return False
        row = self.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='conversations'").fetchone()
        return row is not None

    def _backup(self, version):
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
        backup = self.path.with_name(
            self.path.name + f'.v{version}-backup-' + stamp + '-' + uuid.uuid4().hex)
        shutil.copyfile(self.path, backup)
        return backup

    @contextmanager
    def transaction(self):
        self.connection.execute('BEGIN IMMEDIATE')
        try:
            yield self.connection
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise

    def _create_schema(self, connection):
        connection.execute('CREATE TABLE IF NOT EXISTS settings_metadata ('
                           'key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        connection.execute('CREATE TABLE IF NOT EXISTS conversations ('
                           'id TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT \'\','
                           "subject_user_id TEXT NOT NULL DEFAULT 'owner',"
                           'created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)')
        connection.execute('CREATE TABLE IF NOT EXISTS messages ('
                           'id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL '
                           'REFERENCES conversations(id) ON DELETE CASCADE,'
                           "role TEXT NOT NULL CHECK(role IN ('user','assistant','system')) ,"
                           'content TEXT NOT NULL,'
                           "subject_user_id TEXT NOT NULL DEFAULT 'owner',"
                           'created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)')
        self._ensure_subject_columns(connection)
        connection.execute('CREATE INDEX IF NOT EXISTS idx_messages_conversation '
                           'ON messages(conversation_id, created_at)')
        connection.execute('CREATE INDEX IF NOT EXISTS idx_messages_subject '
                           'ON messages(subject_user_id, conversation_id, created_at)')
        connection.execute('CREATE TABLE IF NOT EXISTS conversation_summaries ('
                           'conversation_id TEXT PRIMARY KEY,'
                           "summary TEXT NOT NULL DEFAULT '',"
                           'through_message_id INTEGER NOT NULL DEFAULT 0,'
                           "updated_at TEXT NOT NULL DEFAULT '')")
        connection.execute('CREATE TABLE IF NOT EXISTS memories ('
                           'id TEXT PRIMARY KEY, content TEXT NOT NULL,'
                           'created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,'
                           "subject_user_id TEXT NOT NULL DEFAULT 'owner',"
                           "category TEXT NOT NULL DEFAULT 'general',"
                           'importance REAL NOT NULL DEFAULT 0.5,'
                           "source_type TEXT NOT NULL DEFAULT 'manual',"
                           'source_message_id TEXT,'
                           'updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,'
                           'deleted_at TEXT)')
        self._ensure_memory_columns(connection)
        connection.execute('CREATE INDEX IF NOT EXISTS idx_memories_subject '
                           'ON memories(subject_user_id, created_at)')
        connection.execute('CREATE INDEX IF NOT EXISTS idx_memories_category '
                           'ON memories(subject_user_id, category)')
        connection.execute('CREATE INDEX IF NOT EXISTS idx_memories_content '
                           'ON memories(content)')
        connection.execute('CREATE INDEX IF NOT EXISTS idx_memories_topic '
                           'ON memories(subject_user_id, topic_id)')
        connection.execute('CREATE INDEX IF NOT EXISTS idx_memories_level '
                           'ON memories(memory_level, deleted_at)')
        connection.execute('CREATE TABLE IF NOT EXISTS memory_operations ('
                           'id TEXT PRIMARY KEY, memory_id TEXT,'
                           "operation TEXT NOT NULL, payload TEXT NOT NULL DEFAULT '{}',"
                           "status TEXT NOT NULL DEFAULT 'applied',"
                           'created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)')
        connection.execute('CREATE INDEX IF NOT EXISTS idx_memory_operations_memory '
                           'ON memory_operations(memory_id, created_at)')
        connection.execute('CREATE TABLE IF NOT EXISTS memory_outbox ('
                           'id INTEGER PRIMARY KEY AUTOINCREMENT, memory_id TEXT,'
                           "operation TEXT NOT NULL, payload TEXT NOT NULL DEFAULT '{}',"
                           'attempts INTEGER NOT NULL DEFAULT 0,'
                           'created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,'
                           'delivered_at TEXT)')
        connection.execute('CREATE INDEX IF NOT EXISTS idx_memory_outbox_pending '
                           'ON memory_outbox(delivered_at, id)')
        connection.execute('CREATE TABLE IF NOT EXISTS memory_jobs ('
                           'id TEXT PRIMARY KEY, memory_id TEXT,'
                           "job_type TEXT NOT NULL DEFAULT '',"
                           "status TEXT NOT NULL DEFAULT 'pending',"
                           "payload TEXT NOT NULL DEFAULT '{}',"
                           'attempts INTEGER NOT NULL DEFAULT 0,'
                           'scheduled_at TEXT,'
                           'created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,'
                           'completed_at TEXT)')
        connection.execute('CREATE INDEX IF NOT EXISTS idx_memory_jobs_status '
                           'ON memory_jobs(status, scheduled_at)')
        connection.execute('CREATE INDEX IF NOT EXISTS idx_memory_jobs_memory '
                           'ON memory_jobs(memory_id, job_type)')
        connection.execute('CREATE TABLE IF NOT EXISTS memory_conflicts ('
                           'id TEXT PRIMARY KEY,'
                           'memory_a TEXT NOT NULL,'
                           'memory_b TEXT NOT NULL,'
                           "topic_key TEXT NOT NULL DEFAULT '',"
                           "status TEXT NOT NULL DEFAULT 'open',"
                           "resolution TEXT NOT NULL DEFAULT '',"
                           'created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,'
                           'resolved_at TEXT)')
        connection.execute('CREATE INDEX IF NOT EXISTS idx_memory_conflicts_status '
                           'ON memory_conflicts(status, created_at)')
        connection.execute('CREATE INDEX IF NOT EXISTS idx_memory_conflicts_topic '
                           'ON memory_conflicts(topic_key, status)')
        connection.execute('CREATE TABLE IF NOT EXISTS memory_embeddings ('
                           'memory_id TEXT PRIMARY KEY,'
                           "model TEXT NOT NULL DEFAULT '',"
                           'dim INTEGER NOT NULL DEFAULT 0,'
                           'vector BLOB,'
                           'updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)')
        connection.execute('CREATE INDEX IF NOT EXISTS idx_memory_embeddings_model '
                           'ON memory_embeddings(model)')
        connection.execute('CREATE TABLE IF NOT EXISTS character_state ('
                           f"subject_user_id TEXT PRIMARY KEY DEFAULT '{SUBJECT_USER_ID}',"
                           "payload TEXT NOT NULL DEFAULT '{}',"
                           'updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)')
        connection.execute('CREATE TABLE IF NOT EXISTS growth_candidates ('
                           'id TEXT PRIMARY KEY,'
                           f"subject_user_id TEXT NOT NULL DEFAULT '{SUBJECT_USER_ID}',"
                           "category TEXT NOT NULL DEFAULT 'general',"
                           'content TEXT NOT NULL,'
                           "status TEXT NOT NULL DEFAULT 'pending',"
                           'created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,'
                           'updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)')
        self._ensure_growth_columns(connection)
        connection.execute('CREATE INDEX IF NOT EXISTS idx_growth_candidates_status '
                           'ON growth_candidates(subject_user_id, status)')
        connection.execute('CREATE INDEX IF NOT EXISTS idx_growth_candidates_trait '
                           'ON growth_candidates(subject_user_id, trait, status)')
        connection.execute('CREATE TABLE IF NOT EXISTS personality_history ('
                           'id TEXT PRIMARY KEY,'
                           "trait TEXT NOT NULL DEFAULT '',"
                           'old_value REAL NOT NULL DEFAULT 0,'
                           'new_value REAL NOT NULL DEFAULT 0,'
                           "evidence_ids TEXT NOT NULL DEFAULT '[]',"
                           'rule_version INTEGER NOT NULL DEFAULT 1,'
                           'created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,'
                           'reverted_at TEXT)')
        connection.execute('CREATE INDEX IF NOT EXISTS idx_personality_history_trait '
                           'ON personality_history(trait, created_at)')
        connection.execute('CREATE TABLE IF NOT EXISTS usage_ledger ('
                           'id INTEGER PRIMARY KEY AUTOINCREMENT,'
                           "request_id TEXT NOT NULL DEFAULT '',"
                           "model TEXT NOT NULL DEFAULT '',"
                           'prompt_tokens INTEGER NOT NULL DEFAULT 0,'
                           'completion_tokens INTEGER NOT NULL DEFAULT 0,'
                           'total_tokens INTEGER NOT NULL DEFAULT 0,'
                           'cost REAL NOT NULL DEFAULT 0,'
                           "currency TEXT NOT NULL DEFAULT 'USD',"
                           'created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)')
        connection.execute('CREATE INDEX IF NOT EXISTS idx_usage_ledger_created '
                           'ON usage_ledger(created_at)')
        connection.execute('CREATE TABLE IF NOT EXISTS phrase_usage ('
                           'phrase_id TEXT PRIMARY KEY,'
                           'uses INTEGER NOT NULL DEFAULT 0,'
                           'last_used_at TEXT)')
        connection.execute('CREATE TABLE IF NOT EXISTS autonomy_state ('
                           f"subject_user_id TEXT PRIMARY KEY DEFAULT '{SUBJECT_USER_ID}',"
                           'last_autonomous_at TEXT,'
                           'hourly_count INTEGER NOT NULL DEFAULT 0,'
                           'hourly_window_start TEXT,'
                           'manual_quiet_until TEXT,'
                           'updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)')
        connection.execute('CREATE TABLE IF NOT EXISTS timers ('
                           'id INTEGER PRIMARY KEY,'
                           'due_at TEXT NOT NULL,'
                           "text TEXT NOT NULL DEFAULT '',"
                           'created_at TEXT NOT NULL,'
                           'fired_at TEXT)')
        connection.execute('CREATE INDEX IF NOT EXISTS idx_timers_pending '
                           'ON timers(fired_at, due_at)')

    def _ensure_memory_columns(self, connection):
        existing = {row[1] for row in connection.execute('PRAGMA table_info(memories)')}
        for name, declaration in _MEMORY_ADDITIONS:
            if name not in existing:
                connection.execute(f'ALTER TABLE memories ADD COLUMN {name} {declaration}')

    def _ensure_growth_columns(self, connection):
        existing = {row[1] for row in connection.execute('PRAGMA table_info(growth_candidates)')}
        for name, declaration in _GROWTH_ADDITIONS:
            if name not in existing:
                connection.execute(
                    f'ALTER TABLE growth_candidates ADD COLUMN {name} {declaration}')

    def _ensure_subject_columns(self, connection):
        for table in ('conversations', 'messages'):
            existing = {row[1] for row in connection.execute(f'PRAGMA table_info({table})')}
            if 'subject_user_id' not in existing:
                connection.execute(
                    f'ALTER TABLE {table} ADD COLUMN '
                    "subject_user_id TEXT NOT NULL DEFAULT 'owner'")

    def _setup_fts(self, connection):
        connection.execute('SAVEPOINT memory_fts')
        try:
            connection.execute("CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts "
                               "USING fts5(memory_id UNINDEXED, content, tokenize='unicode61')")
            connection.execute('RELEASE memory_fts')
            return True
        except sqlite3.OperationalError:
            connection.execute('ROLLBACK TO memory_fts')
            connection.execute('RELEASE memory_fts')
            return False

    def _set_metadata(self, connection):
        connection.executemany(
            'INSERT INTO settings_metadata(key,value) VALUES (?,?) '
            'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
            [('settings_schema_version', str(SETTINGS_SCHEMA_VERSION)),
             ('database_schema_version', str(SCHEMA_VERSION)),
             ('fts5_enabled', '1' if self.fts_enabled else '0')])

    def close(self):
        self.connection.close()
