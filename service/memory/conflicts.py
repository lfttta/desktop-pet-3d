"""Contradiction detection and manual resolution for memories.

A conflict is recorded when two active memories share a topic and disagree on a
key field (time, number or negation). Both rows become ``disputed`` so they are
not injected into prompts until the user keeps one side; resolving archives the
other side and leaves one active version.
"""

import sqlite3
import uuid

from service.database import SUBJECT_USER_ID
from service.memory import policy
from service.memory.store import utc_now

STATUS_OPEN = 'open'
STATUS_RESOLVED = 'resolved'
CONFLICT_THRESHOLD = 0.5


def _same_topic(first, second):
    left = first.get('topic_id')
    right = second.get('topic_id')
    if left and right and left == right:
        return True
    return policy.is_same_topic(first.get('content', ''), second.get('content', ''),
                                threshold=CONFLICT_THRESHOLD)


def _conflicting(first, second):
    if policy.normalize_text(first.get('content', '')) == \
            policy.normalize_text(second.get('content', '')):
        return False
    if not _same_topic(first, second):
        return False
    left = policy.key_fields(first.get('content', ''))
    right = policy.key_fields(second.get('content', ''))
    if set(left['numbers']) != set(right['numbers']):
        return True
    if set(left['times']) != set(right['times']):
        return True
    return bool(left['negations']) != bool(right['negations'])


def _has_open_conflict(store, memory_id):
    row = store.connection.execute(
        "SELECT 1 FROM memory_conflicts WHERE status=? "
        "AND (memory_a=? OR memory_b=?) LIMIT 1",
        (STATUS_OPEN, memory_id, memory_id)).fetchone()
    return row is not None


def _existing_pair(store, first_id, second_id):
    row = store.connection.execute(
        "SELECT 1 FROM memory_conflicts WHERE status=? AND "
        "((memory_a=? AND memory_b=?) OR (memory_a=? AND memory_b=?)) LIMIT 1",
        (STATUS_OPEN, first_id, second_id, second_id, first_id)).fetchone()
    return row is not None


def _topic_of(first, second):
    return first.get('topic_id') or second.get('topic_id') or \
        policy.topic_key(first.get('content', ''))


def detect(store, subject_user_id=SUBJECT_USER_ID):
    """Scan active memories and record new conflicts; returns created rows.

    开放冲突一次性批量载入内存集合（替代每对记忆 2 条 SQL，200 条不再
    产生约 4 万次查询）；分页遍历全部活跃记忆，不再被 200 条上限截断。
    候选仍按 ``_same_topic`` 判定，保持与逐对查询完全一致的检出语义。
    """
    rows = list(store.iter_active(subject_user_id))
    disputed = set()
    pairs = set()
    for memory_a, memory_b in store.connection.execute(
            'SELECT memory_a, memory_b FROM memory_conflicts WHERE status=?',
            (STATUS_OPEN,)):
        disputed.add(memory_a)
        disputed.add(memory_b)
        pairs.add((memory_a, memory_b))
        pairs.add((memory_b, memory_a))
    created = []
    for index, first in enumerate(rows):
        if first['id'] in disputed:
            continue
        for second in rows[index + 1:]:
            if second['id'] in disputed:
                continue
            if not _conflicting(first, second):
                continue
            if (first['id'], second['id']) in pairs:
                continue
            conflict_id = uuid.uuid4().hex
            now = utc_now()
            topic_key = _topic_of(first, second)
            try:
                with store.database.transaction() as connection:
                    connection.execute(
                        'INSERT INTO memory_conflicts(id,memory_a,memory_b,topic_key,'
                        'status,resolution,created_at,resolved_at) '
                        'VALUES (?,?,?,?,?,\'\',?,NULL)',
                        (conflict_id, first['id'], second['id'], topic_key,
                         STATUS_OPEN, now))
                    store.update_memory(connection, first['id'], {
                        'status': 'disputed', 'updated_at': now})
                    store.update_memory(connection, second['id'], {
                        'status': 'disputed', 'updated_at': now})
                    store.record_operation(connection, first['id'], 'dispute', {
                        'conflict_id': conflict_id})
                    store.record_operation(connection, second['id'], 'dispute', {
                        'conflict_id': conflict_id})
            except sqlite3.Error:
                continue
            disputed.add(first['id'])
            disputed.add(second['id'])
            pairs.add((first['id'], second['id']))
            pairs.add((second['id'], first['id']))
            created.append({
                'id': conflict_id,
                'memory_a': first['id'],
                'memory_b': second['id'],
                'topic_key': topic_key,
                'status': STATUS_OPEN,
                'created_at': now,
            })
            break
    return created


def list_conflicts(store, status=None, limit=100):
    if status is not None:
        cursor = store.connection.execute(
            'SELECT * FROM memory_conflicts WHERE status=? '
            'ORDER BY created_at DESC, rowid DESC LIMIT ?', (status, limit))
    else:
        cursor = store.connection.execute(
            'SELECT * FROM memory_conflicts ORDER BY created_at DESC, rowid DESC LIMIT ?',
            (limit,))
    result = []
    for row in cursor.fetchall():
        values = {description[0]: value
                  for description, value in zip(cursor.description, row)}
        result.append({
            'id': values.get('id'),
            'memory_a': values.get('memory_a'),
            'memory_b': values.get('memory_b'),
            'topic_key': values.get('topic_key', ''),
            'status': values.get('status', STATUS_OPEN),
            'created_at': values.get('created_at'),
        })
    return result


def _conflict_row(store, conflict_id):
    cursor = store.connection.execute(
        'SELECT * FROM memory_conflicts WHERE id=?', (conflict_id,))
    row = cursor.fetchone()
    if row is None:
        return None
    return {description[0]: value for description, value in zip(cursor.description, row)}


def resolve(store, conflict_id, keep):
    """Keep one side of a conflict and archive the other."""
    if type(conflict_id) is not str or not conflict_id:
        return {'status': 'rejected', 'reason': 'invalid_conflict_id'}
    if keep not in ('a', 'b'):
        return {'status': 'rejected', 'reason': 'invalid_keep'}
    conflict = _conflict_row(store, conflict_id)
    if conflict is None:
        return {'status': 'rejected', 'reason': 'not_found'}

    first_id = conflict['memory_a']
    second_id = conflict['memory_b']
    if conflict.get('status') == STATUS_RESOLVED:
        stored = conflict.get('resolution') or keep
        kept_id = first_id if stored == 'a' else second_id
        archived_id = second_id if stored == 'a' else first_id
        return {'status': 'resolved', 'kept': kept_id, 'archived': archived_id}

    kept_id = first_id if keep == 'a' else second_id
    archived_id = second_id if keep == 'a' else first_id

    keeper = store.get_memory(kept_id)
    loser = store.get_memory(archived_id)
    if keeper is None or keeper.get('deleted_at'):
        return {'status': 'rejected', 'reason': 'not_found'}
    if keeper.get('status') not in ('active', 'disputed'):
        return {'status': 'rejected', 'reason': 'not_available'}

    moved = False
    if loser is not None and not loser.get('deleted_at'):
        try:
            moved = store.archive_body(archived_id)
        except OSError:
            return {'status': 'rejected', 'reason': 'body_move_failed'}

    now = utc_now()
    previous_fuzz = policy.clamp_fuzz_level((loser or {}).get('fuzz_level'))
    try:
        with store.database.transaction() as connection:
            store.update_memory(connection, kept_id, {
                'status': 'active', 'updated_at': now})
            store.update_memory(connection, archived_id, {
                'status': 'archived',
                'fuzz_level': policy.FUZZ_ARCHIVED,
                'updated_at': now,
            })
            connection.execute(
                'UPDATE memory_conflicts SET status=?, resolution=?, resolved_at=? '
                'WHERE id=? AND status=?',
                (STATUS_RESOLVED, keep, now, conflict_id, STATUS_OPEN))
            store.record_operation(connection, archived_id, 'archive', {
                'previous_fuzz_level': previous_fuzz})
            store.record_operation(connection, kept_id, 'conflict_keep', {
                'conflict_id': conflict_id, 'kept': kept_id, 'archived': archived_id})
    except Exception:
        if moved:
            try:
                store.restore_body(archived_id)
            except OSError:
                pass
        raise

    store.remove_index(archived_id)
    store.index_memory(kept_id, keeper.get('content', ''))
    return {'status': 'resolved', 'kept': kept_id, 'archived': archived_id}
