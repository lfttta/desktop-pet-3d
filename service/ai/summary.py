"""Rolling conversation summaries generated off the service thread.

A summary is a short digest of the facts, user preferences and unfinished
items in one conversation. The trigger check and the transcript read run on
the caller thread, which owns the shared SQLite connection. Only the provider
call and the single-row write run on a daemon thread, so a slow or broken
endpoint never delays a chat response.

Messages are identified by ``messages.rowid``. ``through_message_id`` stores
the newest rowid covered by the summary; a summary is regenerated once more
than :data:`MAX_LAG_MESSAGES` newer messages have been appended.

Failures are recorded as event codes and never raised: the summary is an
optional convenience, not part of the chat contract.
"""

import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone

from service.ai.budget import BudgetLedger, reservation
from service.ai.provider import OpenAICompatibleProvider
from service.database import SUBJECT_USER_ID
from service.secrets import SecretsError, load_key_for

MIN_MESSAGES = 24
MAX_LAG_MESSAGES = 12
MAX_SUMMARY_CHARS = 200
MAX_TRANSCRIPT_CHARS = 6000
MAX_MESSAGE_CHARS = 400
# A failed or empty reply backs off exponentially so the next chat turn does
# not immediately retry (and re-bill) the summary provider.
FAILURE_BACKOFF_BASE_SECONDS = 60.0
FAILURE_BACKOFF_MAX_SECONDS = 3600.0
FAILURE_BACKOFF_MAX_DOUBLINGS = 10

SUMMARY_RULES = (
    '你在为一段对话写摘要。要求：200 字以内；只保留事实、用户偏好和未完成事项；'
    '不要编造，不要加入评价、Markdown 或额外说明；直接输出摘要正文。'
)

_LOGGER = logging.getLogger(__name__)
_ACTIVE = set()
_ACTIVE_LOCK = threading.Lock()
# Per-conversation retry state, guarded by _ACTIVE_LOCK. A failure timestamp
# keeps a broken endpoint from being called once per chat turn.
_RETRY_AT = {}
_FAILURES = {}


class _Plan:
    __slots__ = ('messages', 'previous', 'through')

    def __init__(self, messages, previous, through):
        self.messages = messages
        self.previous = previous
        self.through = through


def load_summary(database, conversation_id):
    """Return the stored summary for one conversation, or ``""``.

    Read-only and defensive: a missing database, a non-string conversation id
    or any storage error simply yields an empty string.
    """
    if database is None or type(conversation_id) is not str or not conversation_id:
        return ''
    connection = getattr(database, 'connection', None)
    if connection is None:
        return ''
    try:
        row = connection.execute(
            'SELECT summary FROM conversation_summaries WHERE conversation_id=?',
            (conversation_id,)).fetchone()
    except (sqlite3.Error, AttributeError, TypeError):
        return ''
    if not row or type(row[0]) is not str:
        return ''
    return row[0]


def maybe_summarize(store, conversation_id, settings, provider_factory):
    """Start a background summary when the conversation has fallen behind.

    Returns the daemon :class:`threading.Thread` that was started, or ``None``
    when the trigger condition is not met (or preparation failed). The caller
    never waits for the provider call.
    """
    database = _database_for(store)
    if database is None or type(conversation_id) is not str or not conversation_id:
        return None
    if _retry_blocked(conversation_id):
        return None
    settings = settings if isinstance(settings, dict) else {}
    ai_settings = _ai_settings(settings)
    try:
        plan = _plan(database, conversation_id)
    except (sqlite3.Error, AttributeError, TypeError):
        _record_event(store, 'summary_failed', 'prepare_failed')
        return None
    if plan is None:
        return None
    # The summary is a paid provider call; it must obey the same daily token
    # and monthly cost limits as chat and proactive generation.
    try:
        status = BudgetLedger(database).status(
            ai_settings, reservation(_prompt(plan), ai_settings))
    except Exception:
        status = {}
    if status.get('local_only'):
        _record_event(store, 'summary_skipped',
                      str(status.get('reason') or 'budget'))
        return None
    with _ACTIVE_LOCK:
        if conversation_id in _ACTIVE:
            return None
        _ACTIVE.add(conversation_id)
    thread = threading.Thread(
        target=_run, args=(store, database, conversation_id, plan, settings,
                           provider_factory),
        name='summary-' + conversation_id[:32], daemon=True)
    try:
        thread.start()
    except RuntimeError:
        with _ACTIVE_LOCK:
            _ACTIVE.discard(conversation_id)
        _record_event(store, 'summary_failed', 'start_failed')
        return None
    return thread


def _database_for(store):
    database = getattr(store, 'database', None)
    if database is not None:
        return database
    orchestration = getattr(store, '_chat_orchestrator', None)
    return getattr(orchestration, 'database', None)


def _ai_settings(settings):
    ai_settings = settings.get('ai') if isinstance(settings, dict) else None
    return ai_settings if isinstance(ai_settings, dict) else {}


def _retry_blocked(conversation_id):
    with _ACTIVE_LOCK:
        retry_at = _RETRY_AT.get(conversation_id)
    return retry_at is not None and time.monotonic() < retry_at


def _note_failure(conversation_id):
    """Back off after an empty or failed reply; never retry every turn."""
    with _ACTIVE_LOCK:
        failures = _FAILURES.get(conversation_id, 0) + 1
        _FAILURES[conversation_id] = failures
        doublings = min(failures - 1, FAILURE_BACKOFF_MAX_DOUBLINGS)
        delay = min(FAILURE_BACKOFF_MAX_SECONDS,
                    FAILURE_BACKOFF_BASE_SECONDS * (2 ** doublings))
        _RETRY_AT[conversation_id] = time.monotonic() + delay


def _note_success(conversation_id):
    with _ACTIVE_LOCK:
        _FAILURES.pop(conversation_id, None)
        _RETRY_AT.pop(conversation_id, None)


class _WorkerStorage:
    """Minimal Database stand-in that lets BudgetLedger use its own connection."""

    def __init__(self, connection):
        self.connection = connection

    @contextmanager
    def transaction(self):
        self.connection.execute('BEGIN IMMEDIATE')
        try:
            yield self.connection
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise


def _record_usage(database, model, usage, ai_settings):
    """Record the paid summary call from the worker's own connection."""
    path = getattr(database, 'path', None)
    if path is None or not isinstance(usage, dict) or not usage:
        return
    try:
        connection = sqlite3.connect(str(path), timeout=2.0)
    except sqlite3.Error:
        return
    try:
        BudgetLedger(_WorkerStorage(connection)).record(
            'summary', model, usage, ai_settings)
    except sqlite3.Error:
        pass
    finally:
        connection.close()


def _plan(database, conversation_id):
    """Decide on the caller thread whether a summary is due.

    ``None`` means "not yet": at most :data:`MIN_MESSAGES` messages exist or
    the stored summary is at most :data:`MAX_LAG_MESSAGES` messages behind.
    """
    connection = getattr(database, 'connection', None)
    if connection is None:
        return None
    total, latest = connection.execute(
        'SELECT COUNT(*), MAX(rowid) FROM messages '
        'WHERE conversation_id=? AND subject_user_id=?',
        (conversation_id, SUBJECT_USER_ID)).fetchone()
    if not total or not latest or total <= MIN_MESSAGES:
        return None
    row = connection.execute(
        'SELECT summary, through_message_id FROM conversation_summaries '
        'WHERE conversation_id=?', (conversation_id,)).fetchone()
    previous = row[0] if row and type(row[0]) is str else ''
    through = row[1] if row and type(row[1]) is int else 0
    if through:
        lag = connection.execute(
            'SELECT COUNT(*) FROM messages WHERE conversation_id=? '
            'AND subject_user_id=? AND rowid>?',
            (conversation_id, SUBJECT_USER_ID, through)).fetchone()[0]
    else:
        lag = total
    if lag <= MAX_LAG_MESSAGES:
        return None
    first = through if through else 0
    rows = connection.execute(
        'SELECT role, content FROM messages WHERE conversation_id=? '
        "AND subject_user_id=? AND role IN ('user','assistant') AND rowid>? "
        'ORDER BY rowid', (conversation_id, SUBJECT_USER_ID, first)).fetchall()
    messages = [(role, content) for role, content in rows
                if role in ('user', 'assistant') and type(content) is str
                and content.strip()]
    if not messages:
        return None
    return _Plan(messages, previous, latest)


def _prompt(plan):
    lines = []
    for role, content in plan.messages:
        prefix = '对方：' if role == 'user' else '你：'
        lines.append(prefix + content.strip()[:MAX_MESSAGE_CHARS])
    transcript = '\n'.join(lines)
    if len(transcript) > MAX_TRANSCRIPT_CHARS:
        transcript = transcript[-MAX_TRANSCRIPT_CHARS:]
    parts = []
    if plan.previous.strip():
        parts.append('已有摘要：' + plan.previous.strip()[:MAX_SUMMARY_CHARS])
    parts.append('对话内容：\n' + transcript)
    return [
        {'role': 'system', 'content': SUMMARY_RULES},
        {'role': 'user', 'content': '\n\n'.join(parts)},
    ]


def _run(store, database, conversation_id, plan, settings, provider_factory):
    try:
        text, usage, model = _generate(store, plan, settings, provider_factory)
        _record_usage(database, model, usage, _ai_settings(settings))
        if not text:
            _note_failure(conversation_id)
            _record_event(store, 'summary_failed', 'empty_reply')
            return
        _save(database, conversation_id, text, plan.through)
        _note_success(conversation_id)
        _record_event(store, 'summary_updated', 'through=' + str(plan.through))
    except SecretsError as exc:
        _note_failure(conversation_id)
        _record_event(store, 'summary_failed', str(exc.code))
    except Exception as exc:
        _note_failure(conversation_id)
        _record_event(store, 'summary_failed', type(exc).__name__)
    finally:
        with _ACTIVE_LOCK:
            _ACTIVE.discard(conversation_id)


def _generate(store, plan, settings, provider_factory):
    ai_settings = _ai_settings(settings)
    key = load_key_for(store)
    if provider_factory is not None:
        provider = provider_factory(ai_settings)
    else:
        provider = OpenAICompatibleProvider(ai_settings)
    completion = provider.stream_chat(_prompt(plan), key)
    text = getattr(completion, 'text', '')
    if type(text) is not str:
        text = ''
    else:
        text = ' '.join(text.split())[:MAX_SUMMARY_CHARS]
    usage = getattr(completion, 'usage', {})
    if not isinstance(usage, dict):
        usage = {}
    model = str(getattr(completion, 'model', '') or '')
    return text, usage, model


def _save(database, conversation_id, summary, through):
    """Write the summary from the worker's own short-lived connection.

    The shared connection belongs to the service thread, so a second reader/
    writer with its own busy timeout is the only safe way to persist from the
    daemon thread. One row is written, then the connection is closed.
    """
    path = getattr(database, 'path', None)
    if path is None:
        raise sqlite3.Error('summary storage has no path')
    connection = sqlite3.connect(str(path), timeout=2.0)
    try:
        with connection:
            connection.execute(
                'INSERT INTO conversation_summaries'
                '(conversation_id, summary, through_message_id, updated_at) '
                'VALUES(?,?,?,?) '
                'ON CONFLICT(conversation_id) DO UPDATE SET '
                'summary=excluded.summary, '
                'through_message_id=excluded.through_message_id, '
                'updated_at=excluded.updated_at',
                (conversation_id, summary, int(through), _now()))
    finally:
        connection.close()


def _now():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')


def _record_event(store, code, message=''):
    """Record an event code; message text and credentials never pass here."""
    for owner in (getattr(store, 'log', None), getattr(store, 'events', None)):
        record = getattr(owner, 'record', None)
        if callable(record):
            try:
                level = 'error' if code == 'summary_failed' else 'info'
                record(level, 'summary', code, message)
                return
            except Exception:
                pass
        write = getattr(owner, 'write', None)
        if callable(write):
            try:
                write('summary event=' + code + (' ' + message if message else ''))
                return
            except Exception:
                pass
    _LOGGER.info('summary_event=%s %s', code, message)
