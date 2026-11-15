"""Vector embeddings owned by the memory feature.

Vectors are stored as little-endian float32 blobs in ``memory_embeddings`` and
compared in pure Python with cosine similarity. A stored row only participates
when its model and dimension match the active configuration, so switching the
embedding model degrades retrieval to keyword search instead of returning
meaningless scores. A missing table or a damaged blob is skipped and never
breaks the caller.

候选检索是分块键集全扫（不再是 LIMIT 500 的无序窗口），评分途中用堆只保留
分数最高的 limit 条，所以内存占用与候选规模解耦、与新记忆的可见性无关。
"""

import heapq
import math
import sqlite3
import struct

from service.database import SUBJECT_USER_ID
from service.memory import policy
from service.memory.store import memory_summary, utc_now

VECTOR_FORMAT = 'f'
MAX_DIMENSIONS = 4096
MAX_TEXT_CHARS = 8000
BATCH_SIZE = 16
# rebuild 的键集分页页大小：旧语义是「至多重建这么多行」的硬上限（超出永不
# 嵌入），现改为每页行数——rebuild 循环分页直到扫尽，不再丢行。
MAX_REBUILD_ROWS = 10000
# 候选检索：键集分页每块行数（沿用旧窗口尺寸作块大小），以及单次检索的
# 全扫硬顶——超过则截断并 record_error，防病态大库拖死检索。
CANDIDATE_CHUNK_ROWS = 500
MAX_CANDIDATE_SCAN_ROWS = 100000
LAST_ERROR_KEY = 'memory_embedding_last_error'
SUMMARY_SNIPPET_CHARS = 160


def _valid_dim(dim):
    return type(dim) is int and not isinstance(dim, bool) and 0 < dim <= MAX_DIMENSIONS


def _finite_vector(vector, dim):
    if type(vector) is not list or len(vector) != dim:
        return None
    numbers = []
    for item in vector:
        if isinstance(item, bool) or type(item) not in (int, float):
            return None
        number = float(item)
        if not math.isfinite(number):
            return None
        numbers.append(number)
    return numbers


def pack(vector, dim):
    """Pack one validated vector; returns None when the shape is wrong."""
    numbers = _finite_vector(vector, dim)
    if numbers is None:
        return None
    try:
        return struct.pack('<' + str(dim) + VECTOR_FORMAT, *numbers)
    except struct.error:
        return None


def unpack(blob, dim):
    """Unpack one blob; returns None for a damaged or mismatched row."""
    if not _valid_dim(dim) or not isinstance(blob, (bytes, bytearray, memoryview)):
        return None
    raw = bytes(blob)
    if len(raw) != 4 * dim:
        return None
    try:
        values = struct.unpack('<' + str(dim) + VECTOR_FORMAT, raw)
    except struct.error:
        return None
    if any(not math.isfinite(value) for value in values):
        return None
    return list(values)


def cosine(left, right):
    """Cosine similarity for equal-length finite vectors; None otherwise."""
    if not left or not right or len(left) != len(right):
        return None
    dot = left_norm = right_norm = 0.0
    for a, b in zip(left, right):
        if isinstance(a, bool) or isinstance(b, bool):
            return None
        if type(a) not in (int, float) or type(b) not in (int, float):
            return None
        x = float(a)
        y = float(b)
        if not math.isfinite(x) or not math.isfinite(y):
            return None
        dot += x * y
        left_norm += x * x
        right_norm += y * y
    if left_norm <= 0.0 or right_norm <= 0.0:
        return None
    return dot / (math.sqrt(left_norm) * math.sqrt(right_norm))


def _cosine_unchecked(left, right):
    """热路径余弦：两侧已保证是等长的有限数列表，免去逐元素校验。

    存储向量经 pack()→unpack() 往返，unpack() 保证元素是有限 float；查询
    向量在 rank() 入口做过同等的逐元素校验，所以这里不再重复
    isinstance/isfinite（cosine() 原函数保留，供外部与校验路径使用）。
    点积与范数用 math.sumprod 的 C 累加（Python 3.12+ 标准库，本仓库运行时
    明确只用标准库、不引入 numpy），零向量仍返回 None，与 cosine() 一致。
    """
    left_sq = math.sumprod(left, left)
    right_sq = math.sumprod(right, right)
    if left_sq <= 0.0 or right_sq <= 0.0:
        return None
    return math.sumprod(left, right) / (math.sqrt(left_sq) * math.sqrt(right_sq))


def normalize_vectors(outcome, count, dim):
    """Accept an EmbeddingResult or a bare vector list and validate the shape."""
    vectors = getattr(outcome, 'vectors', outcome)
    if type(vectors) is not list or len(vectors) != count:
        raise ValueError('embedding count does not match the request')
    result = []
    for vector in vectors:
        numbers = _finite_vector(vector, dim)
        if numbers is None:
            raise ValueError('embedding dimension does not match the request')
        result.append(numbers)
    return result


# -- rows ----------------------------------------------------------------


def _rows(cursor):
    return [{description[0]: value for description, value in zip(cursor.description, row)}
            for row in cursor.fetchall()]


def get_vector(store, memory_id, model, dim):
    if not _valid_dim(dim):
        return None
    try:
        cursor = store.connection.execute(
            'SELECT model, dim, vector FROM memory_embeddings WHERE memory_id=?',
            (memory_id,))
        row = cursor.fetchone()
    except sqlite3.Error:
        return None
    if row is None or row[0] != model:
        return None
    stored_dim = row[1] if type(row[1]) is int and not isinstance(row[1], bool) else 0
    if stored_dim != dim:
        return None
    return unpack(row[2], dim)


def upsert(store, memory_id, model, dim, vector, updated_at=None):
    """Write one embedding; a bad vector is ignored instead of raising."""
    blob = pack(vector, dim)
    if blob is None:
        return False
    stamp = updated_at or utc_now()
    try:
        with store.database.transaction() as connection:
            connection.execute(
                'INSERT INTO memory_embeddings(memory_id,model,dim,vector,updated_at) '
                'VALUES (?,?,?,?,?) ON CONFLICT(memory_id) DO UPDATE SET '
                'model=excluded.model, dim=excluded.dim, vector=excluded.vector, '
                'updated_at=excluded.updated_at',
                (memory_id, model, dim, blob, stamp))
    except sqlite3.Error:
        return False
    return True


def delete(store, memory_id):
    try:
        with store.database.transaction() as connection:
            connection.execute('DELETE FROM memory_embeddings WHERE memory_id=?',
                               (memory_id,))
    except sqlite3.Error:
        return False
    return True


def indexed_count(store, model, dim):
    if not model or not _valid_dim(dim):
        return 0
    try:
        row = store.connection.execute(
            'SELECT COUNT(*) FROM memory_embeddings WHERE model=? AND dim=?',
            (model, dim)).fetchone()
    except sqlite3.Error:
        return 0
    return int(row[0] or 0) if row is not None else 0


def total_active(store, subject_user_id=SUBJECT_USER_ID):
    try:
        row = store.connection.execute(
            "SELECT COUNT(*) FROM memories WHERE subject_user_id=? AND status='active' "
            "AND deleted_at IS NULL", (subject_user_id,)).fetchone()
    except sqlite3.Error:
        return 0
    return int(row[0] or 0) if row is not None else 0


# -- last error ----------------------------------------------------------


def record_error(store, message):
    text = str(message or '')[:240]
    try:
        with store.database.transaction() as connection:
            connection.execute(
                'INSERT INTO settings_metadata(key,value) VALUES (?,?) '
                'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                (LAST_ERROR_KEY, text))
    except sqlite3.Error:
        pass


def clear_error(store):
    record_error(store, '')


def last_error(store):
    try:
        row = store.connection.execute(
            'SELECT value FROM settings_metadata WHERE key=?', (LAST_ERROR_KEY,)).fetchone()
    except sqlite3.Error:
        return ''
    if row is None or row[0] is None:
        return ''
    return str(row[0])


# -- rebuild -------------------------------------------------------------


def rebuild(store, model, dim, embed, batch_size=BATCH_SIZE,
            max_rows=MAX_REBUILD_ROWS, subject_user_id=SUBJECT_USER_ID):
    """Embed every active memory; returns counts and the first error code.

    旧实现一次 ``find_active(limit=max_rows)``，第 max_rows 条之后的记忆永远
    不会被嵌入；现改为按 ``m.rowid`` 键集分页直查「当前 model/dim 尚无嵌入的
    active 行」（memories LEFT JOIN memory_embeddings），每页 max_rows 行、
    页内仍按 batch_size 分批调用 embed，循环直到扫尽。失败语义不变：保留已
    写入的批次并返回 error_code；已写入的行下次 rebuild 时因已有嵌入而被
    LEFT JOIN 过滤掉，可从断点续跑。
    """
    indexed = 0
    total = 0
    skipped = 0
    error_code = ''
    last_rowid = 0
    while not error_code:
        cursor = store.connection.execute(
            'SELECT m.rowid AS page_rowid, m.* FROM memories AS m '
            'LEFT JOIN memory_embeddings AS e ON e.memory_id=m.id '
            'AND e.model=? AND e.dim=? '
            'WHERE m.subject_user_id=? AND m.status=\'active\' '
            'AND m.deleted_at IS NULL AND e.memory_id IS NULL '
            'AND m.rowid>? ORDER BY m.rowid LIMIT ?',
            (model, dim, subject_user_id, last_rowid, max_rows))
        page = _rows(cursor)
        if not page:
            break
        total += len(page)
        for start in range(0, len(page), batch_size):
            window = page[start:start + batch_size]
            batch = [row for row in window
                     if isinstance(row.get('content'), str) and row['content'].strip()]
            skipped += len(window) - len(batch)
            if not batch:
                continue
            texts = [row['content'][:MAX_TEXT_CHARS] for row in batch]
            try:
                vectors = normalize_vectors(embed(texts), len(texts), dim)
            except Exception as exc:
                error_code = getattr(exc, 'code', '') or type(exc).__name__[:64]
                record_error(store, error_code)
                break
            written = 0
            for row, vector in zip(batch, vectors):
                if upsert(store, row['id'], model, dim, vector):
                    written += 1
            indexed += written
            if written != len(batch):
                error_code = 'storage_unavailable'
                record_error(store, error_code)
                break
        if error_code:
            break
        last_rowid = page[-1]['page_rowid']
    _prune(store, model, dim)
    if not error_code:
        clear_error(store)
    return {'indexed': indexed, 'total': total, 'skipped': skipped,
            'error': error_code}


def _prune(store, model, dim):
    if not model or not _valid_dim(dim):
        return
    try:
        with store.database.transaction() as connection:
            connection.execute(
                'DELETE FROM memory_embeddings WHERE model=? AND dim=? '
                'AND memory_id NOT IN (SELECT id FROM memories '
                "WHERE status='active' AND deleted_at IS NULL)", (model, dim))
    except sqlite3.Error:
        pass


# -- ranking -------------------------------------------------------------


def _snippet(content, limit=SUMMARY_SNIPPET_CHARS):
    text = (content or '').strip()
    return text if len(text) <= limit else text[:limit].rstrip() + '…'


def candidates(store, model, dim, subject_user_id=SUBJECT_USER_ID):
    """按 e.rowid 键集分块扫出全部可检索候选行（逐行产出的生成器）。

    旧实现是一条无 ORDER BY 的 ``LIMIT MAX_CANDIDATES`` 查询，只返回 rowid
    最靠前的 500 行：库一旦超过 500 条，越新的记忆对向量检索越不可见。现改
    为每块 CANDIDATE_CHUNK_ROWS 行、按 e.rowid 递进的键集分页循环扫到尽，
    rank() 端用堆只保留分数最高的 limit 条，内存与库大小解耦。过滤条件与
    旧实现一致；sqlite3.Error 时停止迭代（首块即错等价于旧的返回 []，中途
    出错则保留已扫到的部分）；扫描行数达到 MAX_CANDIDATE_SCAN_ROWS 硬顶时
    截断并 record_error。
    """
    if not model or not _valid_dim(dim):
        return
    scanned = 0
    last_rowid = 0
    try:
        while True:
            cursor = store.connection.execute(
                'SELECT m.*, e.rowid AS embedding_rowid, '
                'e.vector AS embedding_vector FROM memory_embeddings AS e '
                'JOIN memories AS m ON m.id = e.memory_id '
                'WHERE e.model=? AND e.dim=? AND m.subject_user_id=? '
                "AND m.status='active' AND m.deleted_at IS NULL "
                'AND e.rowid>? ORDER BY e.rowid LIMIT ?',
                (model, dim, subject_user_id, last_rowid, CANDIDATE_CHUNK_ROWS))
            rows = _rows(cursor)
            if not rows:
                return
            truncated = False
            for row in rows:
                if scanned >= MAX_CANDIDATE_SCAN_ROWS:
                    truncated = True
                    break
                last_rowid = row.pop('embedding_rowid')
                scanned += 1
                yield row
            if truncated:
                record_error(store, 'candidate scan truncated at '
                             f'{MAX_CANDIDATE_SCAN_ROWS} rows')
                return
            if len(rows) < CANDIDATE_CHUNK_ROWS:
                return
    except sqlite3.Error:
        return


def rank(store, query_vector, model, dim, limit, subject_user_id=SUBJECT_USER_ID):
    """Return protocol summaries ordered by cosine similarity, best first.

    候选来自 candidates() 的分块全扫，评分用 _cosine_unchecked 单程计算；
    扫描途中用最小堆按「排序键整体优劣」只保留 limit 条（堆序与排序键互为
    取负，完全并列时先扫到的更优，等价于旧实现稳定排序的并列次序），收尾
    再按与旧实现一致的排序键排序后输出，小库上结果逐条一致。
    """
    if type(query_vector) is not list or len(query_vector) != dim:
        return []
    if not _valid_dim(dim):
        return []
    # 旧实现里查询向量的逐元素校验发生在 cosine() 内：任何一个元素非法都会
    # 让所有得分变 None、结果为空。这里等价地前置校验一次。
    for value in query_vector:
        if isinstance(value, bool) or type(value) not in (int, float) \
                or not math.isfinite(value):
            return []
    if limit is None or limit <= 0:
        return []
    heap = []
    order = 0
    for row in candidates(store, model, dim, subject_user_id=subject_user_id):
        vector = unpack(row.pop('embedding_vector', None), dim)
        if vector is None:
            continue
        score = _cosine_unchecked(query_vector, vector)
        if score is None or score <= 0.0:
            continue
        # 元组前五位按「越大越好」排列（与排序键互为取负）；第六位是扫描次序
        # 的取负，让完全并列时先扫到的判优；row 永远不会被比较到（次序唯一）。
        entry = (score,
                 float(row.get('importance') or 0.0),
                 int(row.get('mention_count') or 0),
                 policy.timestamp_value(row.get('last_reinforced_at')),
                 policy.timestamp_value(row.get('updated_at')),
                 -order, row)
        order += 1
        if len(heap) < limit:
            heapq.heappush(heap, entry)
        elif entry > heap[0]:
            heapq.heapreplace(heap, entry)
    scored = sorted(heap, key=lambda item: (
        -item[0],
        -item[1],
        -item[2],
        -item[3],
        -item[4],
        -item[5],
    ))
    results = []
    for entry in scored:
        row = entry[6]
        summary = memory_summary(row)
        summary['snippet'] = _snippet(row.get('content', ''))
        summary['score'] = round(entry[0], 6)
        results.append(summary)
    return results
