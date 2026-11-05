"""Vector embeddings owned by the memory feature.

Vectors are stored as little-endian float32 blobs in ``memory_embeddings`` and
compared in pure Python with cosine similarity. A stored row only participates
when its model and dimension match the active configuration, so switching the
embedding model degrades retrieval to keyword search instead of returning
meaningless scores. A missing table or a damaged blob is skipped and never
breaks the caller.
"""

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
MAX_REBUILD_ROWS = 10000
MAX_CANDIDATES = 500
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

    Already stored rows are replaced per batch, so a failure keeps whatever was
    written earlier instead of emptying the index.
    """
    rows = store.find_active(subject_user_id, limit=max_rows)
    indexed = 0
    skipped = 0
    error_code = ''
    for start in range(0, len(rows), batch_size):
        batch = [row for row in rows[start:start + batch_size]
                 if isinstance(row.get('content'), str) and row['content'].strip()]
        skipped += len(rows[start:start + batch_size]) - len(batch)
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
    _prune(store, model, dim)
    if not error_code:
        clear_error(store)
    return {'indexed': indexed, 'total': len(rows), 'skipped': skipped,
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


def candidates(store, model, dim, limit=MAX_CANDIDATES,
               subject_user_id=SUBJECT_USER_ID):
    if not model or not _valid_dim(dim):
        return []
    try:
        cursor = store.connection.execute(
            'SELECT m.*, e.vector AS embedding_vector FROM memory_embeddings AS e '
            'JOIN memories AS m ON m.id = e.memory_id '
            'WHERE e.model=? AND e.dim=? AND m.subject_user_id=? '
            "AND m.status='active' AND m.deleted_at IS NULL LIMIT ?",
            (model, dim, subject_user_id, limit))
        return _rows(cursor)
    except sqlite3.Error:
        return []


def rank(store, query_vector, model, dim, limit, subject_user_id=SUBJECT_USER_ID):
    """Return protocol summaries ordered by cosine similarity, best first."""
    if type(query_vector) is not list or len(query_vector) != dim:
        return []
    if not _valid_dim(dim):
        return []
    scored = []
    for row in candidates(store, model, dim, subject_user_id=subject_user_id):
        vector = unpack(row.pop('embedding_vector', None), dim)
        if vector is None:
            continue
        score = cosine(query_vector, vector)
        if score is None or score <= 0.0:
            continue
        scored.append((score, row))
    scored.sort(key=lambda item: (
        -item[0],
        -float(item[1].get('importance') or 0.0),
        -int(item[1].get('mention_count') or 0),
        -policy.timestamp_value(item[1].get('last_reinforced_at')),
        -policy.timestamp_value(item[1].get('updated_at')),
    ))
    results = []
    for score, row in scored[:limit]:
        summary = memory_summary(row)
        summary['snippet'] = _snippet(row.get('content', ''))
        summary['score'] = round(score, 6)
        results.append(summary)
    return results
