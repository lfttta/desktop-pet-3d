"""Reminder timers, their commands and the once-only due check.

Timers are stored as UTC ISO timestamps so a reminder survives a service
restart. ``check_due`` is called by the runtime heartbeat on the service
thread: it marks every expired row with ``fired_at`` inside one transaction
and returns the rows that were newly fired, which the hook posts as a single
``timer.due`` event per timer. A malformed timestamp is skipped and a storage
failure is logged instead of escaping the tick loop.
"""

from datetime import datetime, timedelta, timezone
import sqlite3

from service.protocol import ProtocolError

NAME = 'timers'
CAPABILITY = {
    'available': True,
    'reason': '',
    'commands': ['timer.set', 'timer.list', 'timer.cancel'],
}

EVENT = 'timer.due'
MAX_MINUTES = 43200
MAX_TEXT_CHARS = 200
DEFAULT_TEXT = '时间到了'


def _database(ctx, request_id):
    database = getattr(ctx, 'database', None)
    if database is None:
        raise ProtocolError('storage_unavailable', 'Timer storage is unavailable.',
                            request_id)
    return database


def _moment(value=None):
    """Return an aware UTC datetime; naive values are treated as UTC."""
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    raise TypeError('now must be a datetime')


def _parse(value):
    """Parse a stored timestamp; an unreadable value returns None."""
    if type(value) is not str or not value:
        return None
    text = value.strip()
    if text.endswith('Z'):
        text = text[:-1] + '+00:00'
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _timer(row):
    return {
        'id': row[0],
        'due_at': row[1],
        'text': row[2],
        'created_at': row[3],
        'fired_at': row[4],
    }


def create(database, minutes, text=DEFAULT_TEXT, now=None):
    """Insert one reminder due ``minutes`` after ``now`` and return it."""
    moment = _moment(now)
    due_at = (moment + timedelta(minutes=minutes)).isoformat()
    created_at = moment.isoformat()
    with database.transaction() as connection:
        cursor = connection.execute(
            'INSERT INTO timers(due_at, text, created_at) VALUES(?,?,?)',
            (due_at, text, created_at))
        timer_id = cursor.lastrowid
    return {'id': timer_id, 'due_at': due_at, 'text': text,
            'created_at': created_at, 'fired_at': None}


def pending(database):
    """Return unfired reminders in due order."""
    rows = database.connection.execute(
        'SELECT id, due_at, text, created_at, fired_at FROM timers '
        'WHERE fired_at IS NULL ORDER BY due_at, id').fetchall()
    return [_timer(row) for row in rows]


def cancel(database, timer_id=None):
    """Delete pending reminders; without an id every pending timer is removed."""
    with database.transaction() as connection:
        if timer_id is None:
            rows = connection.execute(
                'SELECT id FROM timers WHERE fired_at IS NULL ORDER BY id').fetchall()
            ids = [row[0] for row in rows]
            connection.execute('DELETE FROM timers WHERE fired_at IS NULL')
        else:
            row = connection.execute(
                'SELECT id FROM timers WHERE id=? AND fired_at IS NULL',
                (timer_id,)).fetchone()
            ids = [row[0]] if row is not None else []
            if ids:
                connection.execute('DELETE FROM timers WHERE id=? AND fired_at IS NULL',
                                   (timer_id,))
    return ids


def check_due(database, now=None):
    """Mark expired reminders fired and return the newly fired ones.

    The compare-and-set update makes the transition once-only even if the
    heartbeat runs again before the rows are read back. Unreadable timestamps
    are left pending rather than firing a reminder at the wrong time.
    """
    if database is None:
        return []
    moment = _moment(now)
    stamp = moment.isoformat()
    fired = []
    with database.transaction() as connection:
        rows = connection.execute(
            'SELECT id, due_at, text FROM timers WHERE fired_at IS NULL '
            'ORDER BY due_at, id').fetchall()
        for timer_id, due_at, text in rows:
            due = _parse(due_at)
            if due is None or due > moment:
                continue
            updated = connection.execute(
                'UPDATE timers SET fired_at=? WHERE id=? AND fired_at IS NULL',
                (stamp, timer_id))
            if updated.rowcount:
                fired.append({'id': timer_id, 'due_at': due_at, 'text': text})
    return fired


def tick(runtime, database, now=None):
    """Check due reminders and post one event each; a failure only logs."""
    try:
        due = check_due(database, now=now)
    except (sqlite3.Error, OSError, ValueError, TypeError):
        events = getattr(runtime, 'events', None)
        record = getattr(events, 'record', None)
        if callable(record):
            record('error', 'timers', 'due_check_failed', 'Timer due check failed.')
        return 0
    for item in due:
        try:
            runtime.post('timer', EVENT, {'id': item['id'], 'due_at': item['due_at'],
                                          'text': item['text']})
        except Exception:
            events = getattr(runtime, 'events', None)
            record = getattr(events, 'record', None)
            if callable(record):
                record('error', 'timers', 'due_post_failed', 'Timer event post failed.')
    return len(due)


def register_tick(runtime, database):
    """Attach the due check to a runtime heartbeat; returns the hook."""
    if database is None:
        return None

    def hook(_now):
        # The runtime heartbeat passes a monotonic float; reminders compare
        # against wall-clock time so they survive restarts and clock format.
        tick(runtime, database)

    runtime.register_tick(hook)
    return hook


def set_timer(request, store, ctx):
    payload = request['payload']
    minutes = payload.get('minutes')
    if type(minutes) is not int or not 1 <= minutes <= MAX_MINUTES:
        raise ProtocolError(
            'invalid_request',
            f'timer.set requires integer minutes from 1 to {MAX_MINUTES}.',
            request['id'])
    text = payload.get('text', DEFAULT_TEXT)
    if text is None:
        text = DEFAULT_TEXT
    if type(text) is not str:
        raise ProtocolError('invalid_request', 'timer.set requires text to be a string.',
                            request['id'])
    text = text.strip()[:MAX_TEXT_CHARS] or DEFAULT_TEXT
    timer = create(_database(ctx, request['id']), minutes, text)
    return 'result', {'timer': timer}


def list_timers(request, store, ctx):
    timers = pending(_database(ctx, request['id']))
    return 'result', {'timers': timers, 'count': len(timers)}


def cancel_timers(request, store, ctx):
    timer_id = request['payload'].get('id')
    if timer_id is not None and (type(timer_id) is not int or timer_id < 1):
        raise ProtocolError('invalid_request', 'timer.cancel requires a positive timer id.',
                            request['id'])
    ids = cancel(_database(ctx, request['id']), timer_id)
    return 'result', {'cancelled': ids, 'count': len(ids)}


COMMANDS = {
    'timer.set': set_timer,
    'timer.list': list_timers,
    'timer.cancel': cancel_timers,
}
