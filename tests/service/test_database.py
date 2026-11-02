import sqlite3

import pytest


def test_initialization_metadata_empty_tables_and_idempotence(tmp_path):
    from service.database import Database
    db = Database(tmp_path / 'pet.db')
    assert dict(db.connection.execute('SELECT key,value FROM settings_metadata')) == {
        'settings_schema_version': '1', 'database_schema_version': '1',
    }
    for table in ('conversations', 'messages', 'memories'):
        assert db.connection.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] == 0
    db.close()
    db = Database(tmp_path / 'pet.db')
    assert db.connection.execute('PRAGMA user_version').fetchone() == (1,)
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
