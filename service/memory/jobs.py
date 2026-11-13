"""Durable memory maintenance queue.

Jobs live in ``memory_jobs`` so a restart never loses pending work. The runtime
tick calls :func:`tick`; work only starts when the queue is large enough, the
user has been idle for a while, or it is the configured quiet window. Failed
jobs are retried with a bounded attempt count, and every handler leaves the
original memory untouched when it cannot finish.
"""

from datetime import datetime, timedelta, timezone
import json
import sqlite3
import uuid

from service.database import SUBJECT_USER_ID
from service.memory import compact, policy, writer
from service.memory.store import utc_now

JOB_DEDUPE = 'dedupe'
JOB_FORMAT = 'format'
JOB_COMPACT = 'compact'
JOB_ARCHIVE = 'archive'
JOB_TYPES = (JOB_DEDUPE, JOB_FORMAT, JOB_COMPACT, JOB_ARCHIVE)

STATUS_PENDING = 'pending'
STATUS_RUNNING = 'running'
STATUS_FAILED = 'failed'
STATUS_COMPLETED = 'completed'
STATUSES = (STATUS_PENDING, STATUS_RUNNING, STATUS_FAILED, STATUS_COMPLETED)

QUEUE_THRESHOLD = 5
IDLE_MINUTES = 10
MAX_ATTEMPTS = 3
BATCH_LIMIT = 10
MAX_PER_SCAN = 20
RETRY_BASE_SECONDS = 60
STALE_RUNNING_SECONDS = 300

JOB_COLUMNS = ('id', 'memory_id', 'job_type', 'status', 'payload', 'attempts',
               'scheduled_at', 'created_at', 'completed_at')

_PAUSED = set()


def _key(store):
    path = getattr(store, 'path', None)
    return str(path) if path is not None else str(id(store))


def pause(store):
    _PAUSED.add(_key(store))


def resume(store):
    _PAUSED.discard(_key(store))


def is_paused(store):
    return _key(store) in _PAUSED


def _memory_settings(settings):
    if isinstance(settings, dict) and isinstance(settings.get('memory'), dict):
        return settings['memory']
    return settings if isinstance(settings, dict) else {}


def _group(settings, name):
    if isinstance(settings, dict) and isinstance(settings.get(name), dict):
        return settings[name]
    return {}


def _moment(now=None):
    if now is None:
        return datetime.now(timezone.utc)
    if isinstance(now, datetime):
        return now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    if isinstance(now, str):
        parsed = policy.parse_timestamp(now)
        return parsed if parsed is not None else datetime.now(timezone.utc)
    if isinstance(now, (int, float)) and not isinstance(now, bool):
        # Runtime ticks pass a monotonic float, which cannot be mapped to wall
        # clock time; maintenance scheduling uses wall clock instead.
        return datetime.now(timezone.utc)
    return datetime.now(timezone.utc)


def _iso(moment):
    return moment.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')


def _payload(job):
    raw = job.get('payload') if isinstance(job, dict) else None
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw or '{}')
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _error_code(exc):
    code = getattr(exc, 'code', '')
    if isinstance(code, str) and code:
        return code[:64]
    return type(exc).__name__[:64]


# -- queue ---------------------------------------------------------------


def _pending_like(connection, memory_id, job_type):
    if memory_id is None:
        return None
    return connection.execute(
        "SELECT 1 FROM memory_jobs WHERE memory_id=? AND job_type=? "
        "AND status IN ('pending','running') LIMIT 1",
        (memory_id, job_type)).fetchone()


def enqueue(store, memory_id, job_type, payload=None, scheduled_at=None):
    """Insert one pending job; repeated requests for the same memory coalesce."""
    if job_type not in JOB_TYPES:
        raise ValueError('unsupported memory job type')
    job_id = uuid.uuid4().hex
    now = utc_now()
    at = scheduled_at or now
    with store.database.transaction() as connection:
        if _pending_like(connection, memory_id, job_type) is not None:
            return None
        connection.execute(
            'INSERT INTO memory_jobs(id,memory_id,job_type,status,payload,attempts,'
            'scheduled_at,created_at,completed_at) VALUES (?,?,?,?,?,0,?,?,NULL)',
            (job_id, memory_id, job_type, STATUS_PENDING,
             json.dumps(payload or {}, ensure_ascii=False), at, now))
    return job_row(store, job_id)


def job_row(store, job_id):
    cursor = store.connection.execute('SELECT * FROM memory_jobs WHERE id=?', (job_id,))
    row = cursor.fetchone()
    if row is None:
        return None
    return {description[0]: value for description, value in zip(cursor.description, row)}


def list_jobs(store, status=None, limit=50):
    if status is not None and status not in STATUSES:
        return []
    if status is None:
        cursor = store.connection.execute(
            'SELECT * FROM memory_jobs ORDER BY created_at, rowid LIMIT ?', (limit,))
    else:
        cursor = store.connection.execute(
            'SELECT * FROM memory_jobs WHERE status=? ORDER BY created_at, rowid LIMIT ?',
            (status, limit))
    return [{description[0]: value for description, value in zip(cursor.description, row)}
            for row in cursor.fetchall()]


def _last_error(connection):
    cursor = connection.execute(
        "SELECT payload FROM memory_jobs WHERE status='failed' "
        "ORDER BY completed_at DESC, rowid DESC LIMIT 1")
    row = cursor.fetchone()
    if row is None:
        return ''
    try:
        payload = json.loads(row[0] or '{}')
    except (TypeError, ValueError):
        return ''
    error = payload.get('error') if isinstance(payload, dict) else ''
    return error if isinstance(error, str) else ''


def counts(store):
    result = {name: 0 for name in STATUSES}
    for status, count in store.connection.execute(
            'SELECT status, COUNT(*) FROM memory_jobs GROUP BY status'):
        if status in result:
            result[status] = int(count)
    result['last_error'] = _last_error(store.connection)
    return result


status_counts = counts


# -- scheduling ----------------------------------------------------------


def _last_user_message_at(store):
    try:
        row = store.connection.execute(
            "SELECT created_at FROM messages WHERE role='user' AND subject_user_id=? "
            "ORDER BY created_at DESC LIMIT 1", (SUBJECT_USER_ID,)).fetchone()
    except sqlite3.Error:
        return None
    return policy.parse_timestamp(row[0]) if row is not None else None


def _in_quiet_hours(settings, moment):
    autonomy = _group(settings, 'autonomy')
    start = autonomy.get('quiet_start')
    end = autonomy.get('quiet_end')
    if type(start) is not str or type(end) is not str or start == end:
        return False
    try:
        start_hour, start_minute = (int(part) for part in start.split(':', 1))
        end_hour, end_minute = (int(part) for part in end.split(':', 1))
    except (TypeError, ValueError):
        return False
    local = moment.astimezone()
    minute = local.hour * 60 + local.minute
    low = start_hour * 60 + start_minute
    high = end_hour * 60 + end_minute
    if low == high:
        return False
    if low < high:
        return low <= minute < high
    return minute >= low or minute < high


def should_run(store, settings, now=None, pending_count=0):
    """True when the maintenance window is open."""
    if is_paused(store):
        return False
    if pending_count >= QUEUE_THRESHOLD:
        return True
    moment = _moment(now)
    if _in_quiet_hours(settings, moment):
        return True
    last = _last_user_message_at(store)
    if last is None:
        return True
    return (moment - last).total_seconds() >= IDLE_MINUTES * 60


def _recover_stale(store, moment):
    cutoff = moment.timestamp() - STALE_RUNNING_SECONDS
    rows = store.connection.execute(
        "SELECT id, scheduled_at FROM memory_jobs WHERE status='running'").fetchall()
    stale = [row[0] for row in rows
             if policy.timestamp_value(row[1]) < cutoff]
    if not stale:
        return
    with store.database.transaction() as connection:
        for job_id in stale:
            connection.execute(
                "UPDATE memory_jobs SET status='pending', scheduled_at=? "
                "WHERE id=? AND status='running'", (_iso(moment), job_id))


# -- processing ----------------------------------------------------------


def _mark_running(store, job_id, moment):
    with store.database.transaction() as connection:
        cursor = connection.execute(
            "UPDATE memory_jobs SET status='running', attempts=attempts+1, scheduled_at=? "
            "WHERE id=? AND status='pending'", (_iso(moment), job_id))
        return cursor.rowcount == 1


def _mark_completed(store, job_id, moment, result=None):
    with store.database.transaction() as connection:
        row = connection.execute('SELECT payload FROM memory_jobs WHERE id=?',
                                 (job_id,)).fetchone()
        payload = _payload({'payload': row[0]}) if row is not None else {}
        if result is not None:
            payload['result'] = result
        connection.execute(
            "UPDATE memory_jobs SET status='completed', completed_at=?, payload=? "
            "WHERE id=?", (_iso(moment), json.dumps(payload, ensure_ascii=False), job_id))


def _mark_retry(store, job_id, moment, error):
    with store.database.transaction() as connection:
        row = connection.execute('SELECT attempts, payload FROM memory_jobs WHERE id=?',
                                 (job_id,)).fetchone()
        if row is None:
            return
        attempts = int(row[0] or 0)
        payload = _payload({'payload': row[1]})
        payload['error'] = error
        payload['attempts'] = attempts
        if attempts >= MAX_ATTEMPTS:
            connection.execute(
                "UPDATE memory_jobs SET status='failed', completed_at=?, payload=? "
                "WHERE id=?", (_iso(moment), json.dumps(payload, ensure_ascii=False), job_id))
        else:
            scheduled = _iso(moment + timedelta(seconds=RETRY_BASE_SECONDS * attempts))
            connection.execute(
                "UPDATE memory_jobs SET status='pending', scheduled_at=?, payload=? "
                "WHERE id=?", (scheduled, json.dumps(payload, ensure_ascii=False), job_id))


def _run_dedupe(store, memory_id, moment):
    row = store.get_memory(memory_id)
    if row is None or row.get('deleted_at') or row.get('status') != 'active':
        return {'status': 'skipped', 'reason': 'not_active'}
    key = policy.normalize_text(row.get('content', ''))
    if not key:
        return {'status': 'skipped', 'reason': 'empty'}
    # 精确重复候选走 dedupe_key 索引；键截断到 200 字符，命中后全文复核。
    duplicates = [item for item in store.find_by_dedupe_key(key[:200])
                  if item['id'] != memory_id
                  and policy.normalize_text(item.get('content', '')) == key]
    if not duplicates:
        return {'status': 'skipped', 'reason': 'no_duplicate'}
    group = duplicates + [row]
    keeper = max(group, key=lambda item: (
        int(item.get('mention_count') or 1),
        -policy.timestamp_value(item.get('created_at')),
    ))
    if keeper['id'] == memory_id:
        return {'status': 'skipped', 'reason': 'keeper'}

    moved = store.archive_body(memory_id)
    now = utc_now()
    mention_count = int(keeper.get('mention_count') or 1) + 1
    session_count = int(keeper.get('session_count') or 1) + 1
    importance = policy.merge_importance(
        keeper.get('importance'), row.get('importance'), mention_count - 1)
    confidence = policy.merge_confidence(
        keeper.get('confidence'), row.get('confidence'), mention_count - 1)
    strength = policy.strength(importance, mention_count, session_count,
                               keeper.get('recall_count') or 0, confidence)
    try:
        with store.database.transaction() as connection:
            store.update_memory(connection, memory_id, {
                'status': 'superseded',
                'superseded_by': keeper['id'],
                'updated_at': now,
            })
            store.update_memory(connection, keeper['id'], {
                'mention_count': mention_count,
                'session_count': session_count,
                'importance': importance,
                'confidence': confidence,
                'strength': strength,
                'last_reinforced_at': now,
                'updated_at': now,
            })
            store.record_operation(connection, memory_id, 'dedupe', {
                'superseded_by': keeper['id']})
    except Exception:
        if moved:
            try:
                store.restore_body(memory_id)
            except OSError:
                pass
        raise
    store.remove_index(memory_id)
    return {'status': 'superseded', 'kept': keeper['id']}


def _run_archive(store, settings, moment, topic_key=None):
    config = _memory_settings(settings).get('archive', {})
    if not config.get('enabled', True):
        return {'status': 'skipped', 'reason': 'disabled'}
    stale_days = int(config.get('topic_stale_days', 14))
    cutoff = moment.timestamp() - stale_days * 86400
    # 分页遍历全部活跃记忆，主题分组不再被 200 条上限截断。
    groups = store.group_by_topic(list(store.iter_active()))
    if topic_key is not None:
        groups = {topic_key: groups.get(topic_key, [])}
    archived = 0
    for members in groups.values():
        if not members:
            continue
        latest = max(policy.timestamp_value(item.get('updated_at')
                                            or item.get('created_at')) for item in members)
        if latest >= cutoff:
            continue
        for item in members:
            if int(item.get('protected') or 0):
                continue
            outcome = writer.archive(store, item['id'])
            if outcome.get('status') == writer.REJECTED:
                if outcome.get('reason') == 'body_move_failed':
                    raise OSError('archive body move failed')
                continue
            archived += 1
    return {'status': 'archived', 'archived': archived}


def run_job(store, job, settings, moment, compressor=None):
    job_type = job.get('job_type')
    payload = _payload(job)
    memory_id = job.get('memory_id')
    if job_type == JOB_COMPACT:
        return compact.process_memory(store, memory_id, settings, moment,
                                      allow_model=True, compressor=compressor)
    if job_type == JOB_FORMAT:
        return compact.process_memory(store, memory_id, settings, moment,
                                      level_limit=1, allow_model=False)
    if job_type == JOB_DEDUPE:
        return _run_dedupe(store, memory_id, moment)
    if job_type == JOB_ARCHIVE:
        return _run_archive(store, settings, moment, payload.get('topic_key'))
    raise ValueError('unsupported memory job type')


def process(store, settings, now=None, limit=BATCH_LIMIT, compressor=None):
    """Run due pending jobs up to ``limit``; returns per-job outcomes."""
    moment = _moment(now)
    limited = max(1, int(limit))
    rows = store.connection.execute(
        "SELECT * FROM memory_jobs WHERE status='pending' AND attempts < ? "
        "ORDER BY scheduled_at, created_at, rowid LIMIT ?",
        (MAX_ATTEMPTS, limited * 4)).fetchall()
    processed = []
    for raw in rows:
        if len(processed) >= limited:
            break
        job = dict(zip(JOB_COLUMNS, raw))
        if policy.timestamp_value(job.get('scheduled_at')) > moment.timestamp():
            continue
        if not _mark_running(store, job['id'], moment):
            continue
        try:
            result = run_job(store, job, settings, moment, compressor=compressor)
        except Exception as exc:
            _mark_retry(store, job['id'], moment, _error_code(exc))
            processed.append({'id': job['id'], 'memory_id': job.get('memory_id'),
                              'job_type': job.get('job_type'), 'status': 'retry',
                              'error': _error_code(exc)})
        else:
            _mark_completed(store, job['id'], moment, result)
            processed.append({'id': job['id'], 'memory_id': job.get('memory_id'),
                              'job_type': job.get('job_type'), 'status': 'completed',
                              'result': result})
    return processed


# -- scanning ------------------------------------------------------------


def _has_pending(store, memory_id, job_type):
    return store.connection.execute(
        "SELECT 1 FROM memory_jobs WHERE memory_id=? AND job_type=? "
        "AND status IN ('pending','running') LIMIT 1",
        (memory_id, job_type)).fetchone() is not None


def _has_topic_job(store, topic_key):
    rows = store.connection.execute(
        "SELECT payload FROM memory_jobs WHERE job_type=? "
        "AND status IN ('pending','running')", (JOB_ARCHIVE,)).fetchall()
    for row in rows:
        if _payload({'payload': row[0]}).get('topic_key') == topic_key:
            return True
    return False


def _scan(store, settings, moment):
    memory = _memory_settings(settings)
    compaction = memory.get('compaction', {})
    archive = memory.get('archive', {})
    queued = 0

    if compaction.get('enabled'):
        remaining = max(0, compact.daily_limit(settings) - compact.daily_usage(store, moment))
        # 分页遍历全部活跃记忆，而不是只看最近更新的 200 条。
        for row in store.iter_active():
            if queued >= MAX_PER_SCAN or remaining <= 0:
                break
            if not compact.eligible(store, row, settings, moment):
                continue
            level = int(row.get('fuzz_level') or 0)
            job_type = JOB_FORMAT if level == 0 else JOB_COMPACT
            if _has_pending(store, row['id'], job_type):
                continue
            if enqueue(store, row['id'], job_type, {'level': level + 1},
                       scheduled_at=_iso(moment)) is not None:
                queued += 1
                if job_type == JOB_COMPACT:
                    remaining -= 1

        # 精确去重用一条 GROUP BY 找出重复键，替代 O(n²) 两两比较。
        for key in store.duplicate_dedupe_keys():
            if queued >= MAX_PER_SCAN:
                break
            for duplicate in store.find_by_dedupe_key(key):
                if queued >= MAX_PER_SCAN:
                    break
                if _has_pending(store, duplicate['id'], JOB_DEDUPE):
                    continue
                if enqueue(store, duplicate['id'], JOB_DEDUPE,
                           scheduled_at=_iso(moment)) is not None:
                    queued += 1

    if archive.get('enabled', True):
        stale_days = int(archive.get('topic_stale_days', 14))
        cutoff = moment.timestamp() - stale_days * 86400
        groups = {}
        for item in store.iter_active():
            if int(item.get('protected') or 0):
                continue
            groups.setdefault(store.topic_key_for(item), []).append(item)
        for key, members in groups.items():
            if queued >= MAX_PER_SCAN or not members:
                break
            latest = max(policy.timestamp_value(item.get('updated_at')
                                                or item.get('created_at'))
                         for item in members)
            if latest >= cutoff or _has_topic_job(store, key):
                continue
            if enqueue(store, None, JOB_ARCHIVE, {'topic_key': key},
                       scheduled_at=_iso(moment)) is not None:
                queued += 1
    return queued


def tick(store, settings, now=None, force=False, compressor=None):
    """One maintenance heartbeat; performs work only inside the run window."""
    moment = _moment(now)
    if is_paused(store):
        return []
    _recover_stale(store, moment)
    pending = counts(store)['pending']
    if not force and not should_run(store, settings, moment, pending):
        return []
    _scan(store, settings, moment)
    return process(store, settings, moment, compressor=compressor)
