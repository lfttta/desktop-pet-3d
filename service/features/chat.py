"""Chat commands: persistence, streaming hand-off and cancellation.

``chat.send`` validates and stores the user turn synchronously, then returns
``accepted`` while a worker thread performs the network call. Assistant output
is persisted on the service thread when the terminal event is drained.
"""

import sqlite3
import uuid

from service.ai.orchestrator import get_orchestrator
from service.ai.prompt import build_messages
from service.ai.summary import maybe_summarize
from service.behavior import budget
from service.database import SUBJECT_USER_ID
from service.protocol import ProtocolError

MAX_TEXT_LENGTH = 8000
MAX_CONVERSATION_ID_LENGTH = 128
MAX_HISTORY_LIMIT = 200
DEFAULT_HISTORY_LIMIT = 50

NAME = 'chat'
CAPABILITY = {
    'available': True,
    'reason': '',
    'commands': ['chat.send', 'chat.cancel', 'chat.history'],
}


def _validate(payload, request_id):
    text = payload.get('text')
    if type(text) is not str or not text.strip():
        raise ProtocolError('invalid_request', 'chat.send requires non-empty text.', request_id)
    if len(text) > MAX_TEXT_LENGTH:
        raise ProtocolError('invalid_request', 'chat.send text exceeds 8000 characters.', request_id)
    conversation_id = payload.get('conversation_id', '')
    if type(conversation_id) is not str or len(conversation_id) > MAX_CONVERSATION_ID_LENGTH:
        raise ProtocolError('invalid_request', 'conversation_id must be a string of at most 128 characters.',
                            request_id)
    return text, conversation_id


def _database(store, ctx):
    database = getattr(ctx, 'database', None)
    if database is None:
        database = getattr(store, 'database', None)
    return database


def _begin_exchange(database, conversation_id, text):
    title = text.strip().replace('\n', ' ')[:80]
    with database.transaction() as connection:
        if conversation_id:
            row = connection.execute(
                'SELECT 1 FROM conversations WHERE id=? AND subject_user_id=?',
                (conversation_id, SUBJECT_USER_ID)).fetchone()
            if row is None:
                connection.execute(
                    'INSERT INTO conversations(id, title, subject_user_id) VALUES(?, ?, ?)',
                    (conversation_id, title, SUBJECT_USER_ID))
        else:
            conversation_id = 'c-' + uuid.uuid4().hex
            connection.execute(
                'INSERT INTO conversations(id, title, subject_user_id) VALUES(?, ?, ?)',
                (conversation_id, title, SUBJECT_USER_ID))
        connection.execute(
            'INSERT INTO messages(id, conversation_id, role, content, subject_user_id) '
            'VALUES(?, ?, ?, ?, ?)',
            ('m-' + uuid.uuid4().hex, conversation_id, 'user', text, SUBJECT_USER_ID))
        budget.note_user_activity(connection)
    return conversation_id


def _append_assistant(database, conversation_id, text):
    with database.transaction() as connection:
        connection.execute(
            'INSERT INTO messages(id, conversation_id, role, content, subject_user_id) '
            'VALUES(?, ?, ?, ?, ?)',
            ('m-' + uuid.uuid4().hex, conversation_id, 'assistant', text, SUBJECT_USER_ID))


def send(request, store, ctx):
    text, conversation_id = _validate(request['payload'], request['id'])
    database = _database(store, ctx)
    if database is None:
        raise ProtocolError('storage_unavailable',
                            'Chat storage is not available.', request['id'])
    settings = store.settings
    messages = build_messages(database, conversation_id, text, settings, store)
    try:
        conversation_id = _begin_exchange(database, conversation_id, text)
    except sqlite3.Error:
        raise ProtocolError('storage_unavailable',
                            'The user message could not be stored.', request['id']) from None

    def on_final(kind, data):
        if kind != 'done':
            return
        # A local-only (budget exhausted) or degraded reply did not use the
        # provider normally; spending another paid call on a summary right
        # after it would defeat the budget.
        if isinstance(data, dict) and (data.get('local_only')
                                       or data.get('degraded')):
            return
        content = data.get('text') if isinstance(data, dict) else None
        if type(content) is not str or not content:
            return
        stored = False
        try:
            _append_assistant(database, conversation_id, content)
            stored = True
        except sqlite3.Error:
            pass
        if stored:
            # Trigger check runs here after the assistant row is persisted; the
            # provider call and summary write run on a daemon thread so the
            # terminal chat event is never delayed.
            try:
                maybe_summarize(store, conversation_id, settings,
                                getattr(orchestrator, 'provider_factory', None))
            except Exception:
                pass

    orchestrator = get_orchestrator(store, database)
    orchestrator.attach(ctx)
    request_id = 'chat-' + uuid.uuid4().hex
    try:
        orchestrator.submit(request_id, text, conversation_id, ctx,
                            messages=messages, on_final=on_final)
    except Exception:
        raise ProtocolError('internal_error',
                            'The chat request could not be started.', request['id']) from None
    return 'accepted', {'request_id': request_id,
                        'conversation_id': conversation_id}


def cancel(request, store, ctx):
    request_id = request['payload'].get('request_id', '')
    if request_id != '':
        if type(request_id) is not str or len(request_id) > MAX_CONVERSATION_ID_LENGTH:
            raise ProtocolError('invalid_request',
                                'request_id must be a string of at most 128 characters.',
                                request['id'])
        get_orchestrator(store).cancel(request_id)
    return 'cancelled', {}


def history(request, store, ctx):
    payload = request['payload']
    conversation_id = payload.get('conversation_id', '')
    if type(conversation_id) is not str or len(conversation_id) > MAX_CONVERSATION_ID_LENGTH:
        raise ProtocolError('invalid_request', 'conversation_id must be a string of at most 128 characters.',
                            request['id'])
    limit = payload.get('limit', DEFAULT_HISTORY_LIMIT)
    if type(limit) is not int or not 1 <= limit <= MAX_HISTORY_LIMIT:
        raise ProtocolError('invalid_request', 'limit must be an integer from 1 to 200.', request['id'])
    database = _database(store, ctx)
    if database is None:
        raise ProtocolError('storage_unavailable', 'Chat storage is not available.', request['id'])
    try:
        if not conversation_id:
            row = database.connection.execute(
                'SELECT conversation_id FROM messages WHERE subject_user_id=? '
                'ORDER BY rowid DESC LIMIT 1',
                (SUBJECT_USER_ID,)).fetchone()
            if row is not None:
                conversation_id = row[0]
        rows = []
        if conversation_id:
            rows = database.connection.execute(
                'SELECT id, role, content, created_at FROM messages '
                'WHERE conversation_id=? AND subject_user_id=? '
                'ORDER BY rowid DESC LIMIT ?',
                (conversation_id, SUBJECT_USER_ID, limit)).fetchall()
    except sqlite3.Error:
        raise ProtocolError('storage_unavailable',
                            'Chat history could not be read.', request['id']) from None
    result = []
    for message_id, role, content, created_at in reversed(rows):
        result.append({
            'id': message_id,
            'role': role,
            'content': content,
            'created_at': created_at,
        })
    return 'result', {'conversation_id': conversation_id, 'messages': result}


COMMANDS = {'chat.send': send, 'chat.cancel': cancel, 'chat.history': history}
