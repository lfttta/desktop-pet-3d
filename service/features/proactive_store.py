"""Persist a proactive line as a normal assistant turn in the chat history.

Proactive bubbles used to be invisible to the next prompt, so the character
could not refer to what it had just said. This module appends one assistant
message to the most recently active conversation, creating a conversation when
the user has never chatted. It runs on the service thread and shares the one
SQLite connection; callers treat ``None`` as "history was not written" and must
keep delivering the bubble.

Nothing here logs message bodies: failures return ``None`` and the scheduler
records an event code.
"""

import sqlite3
import uuid

from service.database import SUBJECT_USER_ID

DEFAULT_TITLE = '主动消息'
MAX_TITLE_CHARS = 80


def _latest_conversation(connection):
    row = connection.execute(
        'SELECT conversation_id FROM messages WHERE subject_user_id=? '
        "AND role IN ('user','assistant') ORDER BY rowid DESC LIMIT 1",
        (SUBJECT_USER_ID,)).fetchone()
    if row is None or not row[0]:
        return ''
    return row[0]


def _ensure_conversation(connection, conversation_id, text):
    exists = connection.execute(
        'SELECT 1 FROM conversations WHERE id=? AND subject_user_id=?',
        (conversation_id, SUBJECT_USER_ID)).fetchone()
    if exists is not None:
        return conversation_id
    title = text.strip().replace('\n', ' ')[:MAX_TITLE_CHARS] or DEFAULT_TITLE
    connection.execute(
        'INSERT INTO conversations(id, title, subject_user_id) VALUES(?, ?, ?)',
        (conversation_id, title, SUBJECT_USER_ID))
    return conversation_id


def append_assistant(database, text):
    """Append ``text`` as an assistant message and return its conversation id.

    The most recently used conversation is reused; when there is none, a new
    conversation is created and titled from the line. Any storage error returns
    ``None`` so the caller can log an event and continue.
    """
    if database is None or type(text) is not str or not text.strip():
        return None
    try:
        with database.transaction() as connection:
            conversation_id = _latest_conversation(connection)
            if not conversation_id:
                conversation_id = 'c-' + uuid.uuid4().hex
            _ensure_conversation(connection, conversation_id, text)
            connection.execute(
                'INSERT INTO messages(id, conversation_id, role, content, '
                'subject_user_id) VALUES(?, ?, ?, ?, ?)',
                ('m-' + uuid.uuid4().hex, conversation_id, 'assistant', text,
                 SUBJECT_USER_ID))
    except (AttributeError, TypeError, ValueError, OSError, sqlite3.Error):
        return None
    return conversation_id
