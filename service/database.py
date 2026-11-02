"""Single-connection schema initialization and explicit SQLite transactions."""

from contextlib import contextmanager
import sqlite3


class Database:
    def __init__(self, path):
        self.connection = sqlite3.connect(path, timeout=1.0)
        try:
            self.connection.execute('PRAGMA foreign_keys=ON')
            self.connection.execute('PRAGMA synchronous=FULL')
            version = self.connection.execute('PRAGMA user_version').fetchone()[0]
            if version not in (0, 1):
                raise sqlite3.DatabaseError('unsupported database schema')
            with self.transaction() as connection:
                connection.execute('CREATE TABLE IF NOT EXISTS settings_metadata ('
                                   'key TEXT PRIMARY KEY, value TEXT NOT NULL)')
                connection.execute('CREATE TABLE IF NOT EXISTS conversations ('
                                   'id TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT \'\','
                                   'created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)')
                connection.execute('CREATE TABLE IF NOT EXISTS messages ('
                                   'id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL '
                                   'REFERENCES conversations(id) ON DELETE CASCADE,'
                                   "role TEXT NOT NULL CHECK(role IN ('user','assistant','system')) ,"
                                   'content TEXT NOT NULL,'
                                   'created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)')
                connection.execute('CREATE TABLE IF NOT EXISTS memories ('
                                   'id TEXT PRIMARY KEY, content TEXT NOT NULL,'
                                   'created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)')
                connection.executemany('INSERT OR IGNORE INTO settings_metadata(key,value) VALUES (?,?)',
                                       [('settings_schema_version', '1'),
                                        ('database_schema_version', '1')])
                connection.execute('PRAGMA user_version=1')
        except BaseException:
            self.connection.close()
            raise

    @contextmanager
    def transaction(self):
        self.connection.execute('BEGIN IMMEDIATE')
        try:
            yield self.connection
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise

    def close(self):
        self.connection.close()
