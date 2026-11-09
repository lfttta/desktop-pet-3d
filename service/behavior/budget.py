"""Autonomous message budget persisted in ``autonomy_state``.

The budget combines user settings with durable scheduling row(s):

* disabled switch and quiet hours from settings;
* minimum interval between messages;
* rolling hourly cap;
* manual quiet timestamp (settings and the row; the later one wins);
* a frequency downgrade after the user ignores an autonomous message.

Ignore detection is derived from timestamps rather than an extra column: when a
message is older than ``IGNORE_WINDOW_SECONDS`` and no user activity has been
recorded since it was sent, the minimum interval doubles and the hourly cap is
halved. ``note_user_activity`` clears the downgrade. All reads and writes are
local; this module never performs network access.
"""

from datetime import datetime, timezone
import math
import sqlite3

from service.database import SUBJECT_USER_ID

IGNORE_WINDOW_SECONDS = 1200
IGNORE_INTERVAL_FACTOR = 2.0
HOUR_SECONDS = 3600
# A per-state factor above one day per configured second is nonsense; keeping
# it also stops a huge finite value from overflowing the seconds conversion.
MAX_CONSTRAINT_FACTOR = 86400.0


def normalize_now(value=None):
    """Return an aware datetime in the clock zone used for quiet hours.

    A missing value uses the local clock; explicit naive values are treated as
    UTC. Stored timestamps are always written in UTC.
    """
    if value is None:
        return datetime.now().astimezone()
    if not isinstance(value, datetime):
        raise TypeError('now must be a datetime')
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _utc_iso(now):
    return now.astimezone(timezone.utc).isoformat()


def _parse_timestamp(value):
    if type(value) is not str or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_timestamp(value):
    """Public wrapper for reading a stored or configured timestamp."""
    return _parse_timestamp(value)


def _parse_time_of_day(value):
    if type(value) is not str or len(value) != 5 or value[2] != ':':
        return None
    try:
        hour, minute = int(value[:2]), int(value[3:])
    except ValueError:
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour * 60 + minute


def in_quiet_hours(now, quiet_start, quiet_end):
    """True when the local clock of ``now`` falls in the configured window."""
    start = _parse_time_of_day(quiet_start)
    end = _parse_time_of_day(quiet_end)
    if start is None or end is None or start == end:
        return False
    current = now.hour * 60 + now.minute
    if start < end:
        return start <= current < end
    return current >= start or current < end


def _connection(connection):
    return getattr(connection, 'connection', connection)


def _row(connection):
    connection = _connection(connection)
    if connection is None:
        return None
    row = connection.execute(
        'SELECT last_autonomous_at, hourly_count, hourly_window_start, '
        'manual_quiet_until, updated_at FROM autonomy_state WHERE subject_user_id=?',
        (SUBJECT_USER_ID,)).fetchone()
    if row is None:
        return None
    return {
        'last_autonomous_at': row[0],
        'hourly_count': row[1] or 0,
        'hourly_window_start': row[2],
        'manual_quiet_until': row[3],
        'updated_at': row[4],
    }


def _manual_quiet_until(config, row):
    candidates = []
    setting = _parse_timestamp(config.get('manual_quiet_until'))
    if setting is not None:
        candidates.append(setting)
    if row is not None:
        stored = _parse_timestamp(row.get('manual_quiet_until'))
        if stored is not None:
            candidates.append(stored)
    if not candidates:
        return None
    return max(candidates)


def _constraints(value):
    """Normalize optional per-state budget tightening.

    ``min_factor`` multiplies the minimum interval; ``max_per_hour`` caps the
    hourly limit from above. Anything unrecognized falls back to "no change".
    """
    if not isinstance(value, dict):
        return 1.0, None
    factor = value.get('min_factor')
    if type(factor) in (int, float) and not isinstance(factor, bool):
        try:
            usable = (math.isfinite(factor) and 0 <= factor <= MAX_CONSTRAINT_FACTOR)
        except (OverflowError, TypeError, ValueError):
            # An integer too large for a float cannot describe a sane factor.
            usable = False
        factor = float(factor) if usable else 1.0
    else:
        factor = 1.0
    cap = value.get('max_per_hour')
    if type(cap) is not int or isinstance(cap, bool) or cap < 0:
        cap = None
    return factor, cap


def last_activity(connection):
    """Return the stored user-activity timestamp, or '' when it is absent."""
    try:
        row = _row(connection)
    except (sqlite3.Error, AttributeError, TypeError):
        return ''
    if row is None:
        return ''
    value = row.get('updated_at')
    return value if type(value) is str else ''


def _apply_constraints(status, constraints):
    """Report the constrained limits, including on early-return decisions."""
    factor, cap = _constraints(constraints)
    if factor != 1.0:
        status['effective_min_seconds'] = int(
            status['effective_min_seconds'] * factor)
    if cap is not None:
        status['effective_max_per_hour'] = min(
            status['effective_max_per_hour'], cap)
    return status


def can_speak(connection, config, now=None, constraints=None):
    """Return the budget decision for one autonomous message.

    ``constraints`` optionally tightens the decision for the current desktop
    state: it multiplies the effective minimum interval and caps the effective
    hourly limit. The tightening is applied after the ignored-message
    downgrade, so both effects stack.
    """
    now = normalize_now(now)
    config = config if isinstance(config, dict) else {}
    row = _row(connection)
    quiet_until = _manual_quiet_until(config, row)
    status = _apply_constraints({
        'allowed': False,
        'reason': 'disabled',
        'ignored': False,
        'quiet_hours': False,
        'manual_quiet_until': quiet_until.isoformat() if quiet_until else '',
        'effective_min_seconds': int(config.get('min_seconds_between') or 0),
        'effective_max_per_hour': int(config.get('max_per_hour') or 0),
        'hourly_count': 0,
        'last_autonomous_at': '',
        'seconds_until_allowed': 0,
    }, constraints)
    if not config.get('enabled'):
        return status

    if _connection(connection) is None:
        status['reason'] = 'storage_unavailable'
        return status

    if quiet_until is not None and now < quiet_until.astimezone(now.tzinfo):
        status['reason'] = 'manual_quiet'
        status['manual_quiet_until'] = quiet_until.isoformat()
        return status

    if in_quiet_hours(now, config.get('quiet_start'), config.get('quiet_end')):
        status['quiet_hours'] = True
        status['reason'] = 'quiet_hours'
        return status

    last_at = _parse_timestamp(row.get('last_autonomous_at')) if row else None
    last_user_at = _parse_timestamp(row.get('updated_at')) if row else None
    ignored = bool(last_at is not None and (
        last_user_at is None or last_at > last_user_at)
        and (now - last_at).total_seconds() >= IGNORE_WINDOW_SECONDS)
    status['ignored'] = ignored
    if last_at is not None:
        status['last_autonomous_at'] = last_at.isoformat()

    base_min = int(config.get('min_seconds_between') or 0)
    base_max = int(config.get('max_per_hour') or 0)
    if ignored:
        effective_min = int(base_min * IGNORE_INTERVAL_FACTOR)
        effective_max = max(1, base_max // 2) if base_max > 0 else 0
    else:
        effective_min = base_min
        effective_max = base_max

    status['effective_min_seconds'] = effective_min
    status['effective_max_per_hour'] = effective_max
    # Applied once here, after the ignored downgrade, so both effects stack.
    _apply_constraints(status, constraints)
    effective_min = status['effective_min_seconds']
    effective_max = status['effective_max_per_hour']

    if base_max <= 0:
        status['reason'] = 'hourly_limit'
        return status

    window_start = _parse_timestamp(row.get('hourly_window_start')) if row else None
    count = 0
    if window_start is not None and (now - window_start).total_seconds() < HOUR_SECONDS:
        count = int(row.get('hourly_count') or 0)
    status['hourly_count'] = count
    if count >= effective_max:
        status['reason'] = 'hourly_limit'
        return status

    if last_at is not None:
        since_last = (now - last_at).total_seconds()
        if since_last < effective_min:
            status['reason'] = 'min_interval'
            status['seconds_until_allowed'] = int(effective_min - since_last)
            return status

    status['allowed'] = True
    status['reason'] = 'allowed'
    return status


def record_autonomous(connection, now=None):
    """Record one delivered autonomous message in the hourly window."""
    now = normalize_now(now)
    connection = _connection(connection)
    if connection is None:
        return
    row = _row(connection)
    window_start = _parse_timestamp(row.get('hourly_window_start')) if row else None
    count = int(row.get('hourly_count') or 0) if row else 0
    if window_start is None or (now - window_start).total_seconds() >= HOUR_SECONDS:
        window_start = now
        count = 0
    manual_quiet = row.get('manual_quiet_until') if row else ''
    last_user_at = row.get('updated_at') if row else ''
    connection.execute(
        'INSERT INTO autonomy_state(subject_user_id, last_autonomous_at, hourly_count, '
        'hourly_window_start, manual_quiet_until, updated_at) VALUES (?, ?, ?, ?, ?, ?) '
        'ON CONFLICT(subject_user_id) DO UPDATE SET '
        'last_autonomous_at=excluded.last_autonomous_at, '
        'hourly_count=excluded.hourly_count, '
        'hourly_window_start=excluded.hourly_window_start',
        (SUBJECT_USER_ID, _utc_iso(now), count + 1, _utc_iso(window_start),
         manual_quiet or '', last_user_at or ''))


def note_user_activity(connection, now=None):
    """Mark user activity so an ignored message no longer downgrades the budget."""
    now = normalize_now(now)
    connection = _connection(connection)
    if connection is None:
        return
    connection.execute(
        'INSERT INTO autonomy_state(subject_user_id, updated_at) VALUES (?, ?) '
        'ON CONFLICT(subject_user_id) DO UPDATE SET updated_at=excluded.updated_at',
        (SUBJECT_USER_ID, _utc_iso(now)))


def set_manual_quiet(connection, until, now=None):
    """Persist a manual quiet boundary (a datetime, or None to clear it)."""
    connection = _connection(connection)
    if connection is None:
        return
    value = _utc_iso(normalize_now(until)) if until is not None else ''
    connection.execute(
        'INSERT INTO autonomy_state(subject_user_id, manual_quiet_until, updated_at) '
        "VALUES (?, ?, '') "
        'ON CONFLICT(subject_user_id) DO UPDATE SET manual_quiet_until=excluded.manual_quiet_until',
        (SUBJECT_USER_ID, value))
