"""Contradiction detection and manual resolution for memories.

A conflict is recorded when two active memories share a topic and disagree on a
key field (time, number or negation). For a pair where both sides are chat
memories, detect() no longer blocks both: the older side is automatically
superseded by the newer one (the Graphiti/Zep rule that new information wins)
and the conflict row is stored as ``resolved`` for traceability. Pairs that
involve a manual/protected side, or whose ``created_at`` cannot be ordered,
still turn both rows ``disputed`` so only the user picks a side; resolving
archives the other side and leaves one active version.
"""

import sqlite3
import uuid

from service.database import SUBJECT_USER_ID
from service.memory import policy
from service.memory.store import utc_now

STATUS_OPEN = 'open'
STATUS_RESOLVED = 'resolved'
CONFLICT_THRESHOLD = 0.5
# detect 现在遍历全部活跃记忆（PR #1 去掉了 200 条上限），配对是 O(N²)。
# 每个配对只做预计算事实的集合比较（约 5µs），这里给一次调用设上限，
# 避免大库把服务线程拖过 memory.conflicts.list 的 10s 超时；超限后本轮
# 停止，未扫到的尾部属已知边界（后续可用 topic_id 索引或后台任务收敛）。
MAX_CONFLICT_PAIRS = 100_000


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


def _topic_of(first, second):
    return first.get('topic_id') or second.get('topic_id') or \
        policy.topic_key(first.get('content', ''))


def _open_conflicts(store):
    """一次 SQL 预取全部 open 冲突，返回 (涉及 id 集合, 双向无序对集合)。

    detect 的配对循环原先对每行、每对各发一次 SQL（O(n²) 次查询，
    200 行实测 617ms）；预取后循环内只查内存集合。
    """
    cursor = store.connection.execute(
        'SELECT memory_a, memory_b FROM memory_conflicts WHERE status=?',
        (STATUS_OPEN,))
    involved = set()
    pairs = set()
    for memory_a, memory_b in cursor.fetchall():
        involved.add(memory_a)
        involved.add(memory_b)
        pairs.add((memory_a, memory_b))
        pairs.add((memory_b, memory_a))
    return involved, pairs


def _row_facts(row):
    """配对判定所需的纯 Python 事实，每行只计算一次。

    bigram 的构造与 :func:`policy._bigrams` 一致（长度不足 2 时把归一化
    文本本身作为唯一元素），这里缓存它是因为 O(n²) 配对不能再每对重算。
    """
    content = row.get('content', '')
    normalized = policy.normalize_text(content)
    if len(normalized) < 2:
        bigrams = {normalized} if normalized else set()
    else:
        bigrams = {normalized[index:index + 2]
                   for index in range(len(normalized) - 1)}
    return {
        'id': row.get('id'),
        'row': row,
        'normalized': normalized,
        'bigrams': bigrams,
        'keys': policy.key_fields(content),
        'topic_id': row.get('topic_id'),
    }


def _facts_same_topic(left, right):
    """镜像 :func:`policy.is_same_topic`（阈值 CONFLICT_THRESHOLD），但使用
    每行预计算的归一化文本与 bigram 集合，避免配对循环内重复正则与分词。"""
    left_text = left['normalized']
    right_text = right['normalized']
    if not left_text or not right_text:
        return False
    if left_text == right_text:
        return True
    shorter, longer = ((left_text, right_text) if len(left_text) <= len(right_text)
                       else (right_text, left_text))
    if shorter in longer and len(shorter) / len(longer) >= policy.CONTAINMENT_RATIO:
        return True
    union = len(left['bigrams'] | right['bigrams'])
    if union == 0:
        return False
    return len(left['bigrams'] & right['bigrams']) / union >= CONFLICT_THRESHOLD


def _facts_conflicting(left, right):
    """与 :func:`_conflicting` 同一判定，输入换成预计算事实。"""
    if left['normalized'] == right['normalized']:
        return False
    if left['topic_id'] and left['topic_id'] == right['topic_id']:
        same_topic = True
    else:
        same_topic = _facts_same_topic(left, right)
    if not same_topic:
        return False
    left_keys = left['keys']
    right_keys = right['keys']
    if set(left_keys['numbers']) != set(right_keys['numbers']):
        return True
    if set(left_keys['times']) != set(right_keys['times']):
        return True
    return bool(left_keys['negations']) != bool(right_keys['negations'])


def _auto_supersede_pair(left, right):
    """两侧同为 chat 且未受保护、created_at 可严格定序时返回 (较新, 较旧)。

    依据 Graphiti/Zep 的矛盾处理方式：新事实胜出，旧一侧软失效并保留可
    回溯历史；失效判定只做时间比较，不引入 LLM 判断谁对谁错。任一侧是
    manual 等人工来源、protected，或时间无法定序（含完全相同），返回
    None 维持双侧 disputed 的人工裁决行为。
    """
    if left['row'].get('source_type') != 'chat' or \
            right['row'].get('source_type') != 'chat':
        return None
    if left['row'].get('protected') or right['row'].get('protected'):
        return None
    left_time = policy.parse_timestamp(left['row'].get('created_at'))
    right_time = policy.parse_timestamp(right['row'].get('created_at'))
    if left_time is None or right_time is None or left_time == right_time:
        return None
    return (left, right) if left_time > right_time else (right, left)


def _record_dispute(store, conflict_id, topic_key, now, first, second):
    """原行为：两侧同时 disputed，等人工裁决。失败返回 None 让配对继续。"""
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
        return None
    return {
        'id': conflict_id,
        'memory_a': first['id'],
        'memory_b': second['id'],
        'topic_key': topic_key,
        'status': STATUS_OPEN,
        'created_at': now,
    }


def _record_auto_resolution(store, conflict_id, topic_key, now, first, second,
                            older_id):
    """chat 对自动裁决：旧一侧置 superseded（正文保留可回溯），冲突行直接
    落为 resolved 并记录被保留侧。失败返回 None 让配对继续。"""
    kept_id = second['id'] if older_id == first['id'] else first['id']
    keep = 'b' if kept_id == second['id'] else 'a'
    try:
        with store.database.transaction() as connection:
            connection.execute(
                'INSERT INTO memory_conflicts(id,memory_a,memory_b,topic_key,'
                'status,resolution,created_at,resolved_at) '
                'VALUES (?,?,?,?,?,?,?,?)',
                (conflict_id, first['id'], second['id'], topic_key,
                 STATUS_RESOLVED, keep, now, now))
            store.update_memory(connection, older_id, {
                'status': 'superseded',
                'superseded_by': kept_id,
                'updated_at': now,
            })
            store.record_operation(connection, older_id, 'supersede', {
                'superseded_by': kept_id, 'reason': 'auto_conflict',
                'conflict_id': conflict_id})
    except sqlite3.Error:
        return None
    # 旧侧退出 active 后同步移除全文索引；失败不影响裁决（检索本身按
    # status='active' 硬过滤）。
    store.remove_index(older_id)
    return {
        'id': conflict_id,
        'memory_a': first['id'],
        'memory_b': second['id'],
        'topic_key': topic_key,
        'status': STATUS_RESOLVED,
        'resolution': keep,
        'created_at': now,
        'kept': kept_id,
        'superseded': older_id,
    }


def detect(store, subject_user_id=SUBJECT_USER_ID):
    """Scan active memories and record new conflicts; returns created rows.

    分页遍历全部活跃记忆（不再被 200 条上限截断）；开头一次 SQL 预取全部
    open 冲突，配对循环用每行预计算的文本事实做纯 Python 比较（替代每行
    每对各发一次 SQL）。语义：两侧均为 chat 且未受保护、created_at 可严格
    定序的冲突对，旧一侧自动 superseded、新一侧保持 active，冲突行记为
    resolved；其余（manual/protected/无法定序）维持双侧 disputed 的人工
    裁决行为。每行每次调用至多卷入一个冲突（与旧的 break 语义一致），多
    版本链依赖 repeated detect 收敛，conflicts_list 每次打开都会调用 detect。
    配对总数受 ``MAX_CONFLICT_PAIRS`` 限制，防止全库 O(N²) 拖住服务线程。
    """
    rows = list(store.iter_active(subject_user_id))
    facts = [_row_facts(row) for row in rows]
    open_ids, open_pairs = _open_conflicts(store)
    created = []
    checked = 0
    for left_index in range(len(facts)):
        left = facts[left_index]
        if left['id'] in open_ids:
            continue
        for right_index in range(left_index + 1, len(facts)):
            right = facts[right_index]
            if right['id'] in open_ids:
                continue
            if (left['id'], right['id']) in open_pairs:
                continue
            if checked >= MAX_CONFLICT_PAIRS:
                return created
            checked += 1
            if not _facts_conflicting(left, right):
                continue
            conflict_id = uuid.uuid4().hex
            now = utc_now()
            topic_key = _topic_of(left['row'], right['row'])
            outcome = _auto_supersede_pair(left, right)
            if outcome is None:
                conflict = _record_dispute(store, conflict_id, topic_key, now,
                                           left, right)
            else:
                conflict = _record_auto_resolution(
                    store, conflict_id, topic_key, now, left, right,
                    outcome[1]['id'])
            if conflict is None:
                continue
            created.append(conflict)
            # 本轮已处理过的行不再进入后续配对，等价于旧实现写入
            # open 冲突后靠 _has_open_conflict 跳过的行为。
            open_ids.add(left['id'])
            open_ids.add(right['id'])
            open_pairs.add((left['id'], right['id']))
            open_pairs.add((right['id'], left['id']))
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
