"""向量检索候选窗口：分块全扫 + 堆式 top-k、fused 余弦、rebuild 全覆盖。

旧实现的两个窗口问题：candidates() 是无 ORDER BY 的 LIMIT 500 查询（库超过
500 条后越新的记忆对向量检索越不可见），rebuild 一次至多
MAX_REBUILD_ROWS 行（超出永不嵌入）。本文件用直插 store.insert_memory 造行
（绕过 writer.propose 的正文 fsync）在大库规模下验证修复，并复刻旧实现做
语义金样对比。

耗时断言基线：500 行 x 1024 维 unpack+逐元素校验 cosine 实测约 132-160ms；
纯标准库约束下（明确不引入 numpy）新的 fused 余弦按「绝对上限 + 相对倍数」
双断言，不写候选分析里的 '<10ms'。
"""

import math
import struct
import time
from datetime import datetime, timedelta, timezone

import pytest

from service.database import Database
from service.memory import embeddings as emb
from service.memory import policy
from service.memory.embeddings import _snippet
from service.memory.store import SUBJECT_USER_ID, MemoryStore, memory_summary

MODEL = 'window-model'
DIM = 4


@pytest.fixture
def store(tmp_path):
    database = Database(tmp_path / 'window.db')
    # 本文件验证的是候选窗口、排序语义与 rebuild 分页覆盖，不是 Database 的
    # 落盘耐久性；synchronous=FULL 下数千行逐行 upsert 纯 fsync 就要分钟级，
    # 对一次性测试库关闭以加速，代码路径（事务、SQL）完全不变。
    database.connection.execute('PRAGMA synchronous=OFF')
    memory_store = MemoryStore(database, data_dir=tmp_path)
    yield memory_store
    database.close()


def _vec(seed, dim=DIM):
    # 与 check_vector2.py 相同的 math.sin 种子式确定性向量
    return [math.sin(seed * 1.7 + k) for k in range(dim)]


def _stamp(index):
    moment = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=index)
    return moment.strftime('%Y-%m-%dT%H:%M:%S.%fZ')


def _insert(store, memory_id, content, *, created_at, updated_at=None,
            importance=0.5, mention_count=1, last_reinforced_at=''):
    record = {
        'id': memory_id,
        'content': content,
        'created_at': created_at,
        'updated_at': updated_at or created_at,
        'subject_user_id': SUBJECT_USER_ID,
        'status': 'active',
        'category': 'general',
        'importance': importance,
        'mention_count': mention_count,
        'last_reinforced_at': last_reinforced_at,
        'source_type': 'manual',
    }
    with store.database.transaction() as connection:
        store.insert_memory(connection, record)
    return memory_id


# -- 参照实现：逐元素校验 cosine + 全量排序的旧 rank（语义金样） -------------


def _reference_cosine(left, right):
    """旧 cosine() 的逐元素校验实现，供金样与耗时对照。"""
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


def _reference_candidates(store, model, dim, limit=500,
                          subject_user_id=SUBJECT_USER_ID):
    """旧 candidates() 的 LIMIT 500 无序窗口查询。"""
    if not model or not emb._valid_dim(dim):
        return []
    cursor = store.connection.execute(
        'SELECT m.*, e.vector AS embedding_vector FROM memory_embeddings AS e '
        'JOIN memories AS m ON m.id = e.memory_id '
        'WHERE e.model=? AND e.dim=? AND m.subject_user_id=? '
        "AND m.status='active' AND m.deleted_at IS NULL LIMIT ?",
        (model, dim, subject_user_id, limit))
    return [{description[0]: value
             for description, value in zip(cursor.description, row)}
            for row in cursor.fetchall()]


def _reference_rank(store, query_vector, model, dim, limit,
                    subject_user_id=SUBJECT_USER_ID):
    """旧 rank() 的完整参照复刻（排序键、score>0 过滤、输出结构）。"""
    if type(query_vector) is not list or len(query_vector) != dim:
        return []
    if not emb._valid_dim(dim):
        return []
    scored = []
    for row in _reference_candidates(store, model, dim,
                                     subject_user_id=subject_user_id):
        vector = emb.unpack(row.pop('embedding_vector', None), dim)
        if vector is None:
            continue
        score = _reference_cosine(query_vector, vector)
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


# -- 1. 窗口修复 -------------------------------------------------------------


def test_newest_and_oldest_memories_are_both_retrievable(store):
    ids = []
    for i in range(601):
        memory_id = f'mem-{i:04d}'
        _insert(store, memory_id, f'memory number {i} tag{i:04d}',
                created_at=_stamp(i))
        assert emb.upsert(store, memory_id, MODEL, DIM, _vec(i))
        ids.append(memory_id)

    # 第 601 条（最新）自身向量查询：旧实现 top1 是窗口内的 tag0137
    ranked = emb.rank(store, _vec(600), MODEL, DIM, limit=10)
    assert len(ranked) == 10
    assert ranked[0]['id'] == ids[600]
    assert ranked[0]['text'].endswith('tag0600')

    # 第 1 条（最旧）自身向量查询同样必须可见
    ranked_oldest = emb.rank(store, _vec(0), MODEL, DIM, limit=10)
    assert len(ranked_oldest) == 10
    assert ranked_oldest[0]['id'] == ids[0]
    assert ranked_oldest[0]['text'].endswith('tag0000')


# -- 2. 排序语义金样 ---------------------------------------------------------


def _build_gold_library(store):
    """50 条小库：同分并列、次键差异、零向量、负相关、完全并列全覆盖。"""
    for i in range(50):
        if i in (3, 7, 19):
            vector = _vec(500)               # 同向量 → 同分并列，靠次键分先后
        elif i in (5, 11):
            vector = [0.0, 0.0, 0.0, 0.0]    # 零向量 → cosine 为 None → 跳过
        elif i in (23, 41):
            vector = [-x for x in _vec(2)]   # 与查询负相关 → score<=0 过滤
        else:
            vector = _vec(i * 13 % 47)       # 种子回绕制造额外的同分并列
        _insert(store, f'gold-{i:02d}', f'gold memory {i:02d} marker',
                created_at=_stamp(i),
                updated_at=_stamp(i * 7 % 50),
                importance=round(0.5 + (i % 5) * 0.1, 2),
                mention_count=1 + i % 3,
                last_reinforced_at=_stamp(i * 3 % 50) if i % 4 == 0 else '')
        assert emb.upsert(store, f'gold-{i:02d}', MODEL, DIM, vector)


@pytest.mark.parametrize('limit', [1, 7, 100])
def test_rank_matches_reference_on_gold_library(store, limit):
    _build_gold_library(store)
    query = _vec(2)
    ranked = emb.rank(store, query, MODEL, DIM, limit=limit)
    reference = _reference_rank(store, query, MODEL, DIM, limit=limit)
    assert [(item['id'], item['score']) for item in ranked] == \
           [(item['id'], item['score']) for item in reference]
    # 输出结构不变：memory_summary 字段 + snippet + score
    if ranked:
        assert set(ranked[0]) == set(reference[0])
        assert ranked[0]['snippet'] == _snippet(ranked[0]['text'])


def test_rank_rejects_bad_query_vectors_like_reference(store):
    _build_gold_library(store)
    assert emb.rank(store, [0.0, 0.0, 0.0, 0.0], MODEL, DIM, limit=5) == []
    assert emb.rank(store, [float('nan'), 1.0, 0.0, 0.0], MODEL, DIM, limit=5) == []
    assert emb.rank(store, [float('inf'), 1.0, 0.0, 0.0], MODEL, DIM, limit=5) == []
    assert emb.rank(store, [True, 1.0, 0.0, 0.0], MODEL, DIM, limit=5) == []
    assert emb.rank(store, [1.0, 2.0], MODEL, DIM, limit=5) == []
    assert emb.rank(store, _vec(2), MODEL, 0, limit=5) == []
    assert emb.rank(store, _vec(2), '', DIM, limit=5) == []


# -- 3. rank 耗时 -------------------------------------------------------------


def test_rank_speed_absolute_cap_and_fused_ratio(store):
    dim = 1024
    rows = 500
    query = [0.001] * dim
    for i in range(rows):
        _insert(store, f'speed-{i:03d}', f'speed memory {i}', created_at=_stamp(i))
        assert emb.upsert(store, f'speed-{i:03d}', MODEL, dim, _vec(i, dim))
    blobs = [row[0] for row in store.connection.execute(
        'SELECT vector FROM memory_embeddings ORDER BY rowid').fetchall()]
    assert len(blobs) == rows

    # 相对断言：同一批 blob 上「unpack + 逐元素校验 cosine」参照实现 vs
    # 「unpack + fused 单程余弦」，各取 3 次最优（CPU 密集计时取 min 最稳）。
    reference_times = []
    fused_times = []
    for _ in range(3):
        t0 = time.perf_counter()
        for blob in blobs:
            _reference_cosine(query, emb.unpack(blob, dim))
        reference_times.append(time.perf_counter() - t0)
        t0 = time.perf_counter()
        for blob in blobs:
            emb._cosine_unchecked(query, emb.unpack(blob, dim))
        fused_times.append(time.perf_counter() - t0)
    reference = min(reference_times)
    fused = min(fused_times)
    assert reference / fused >= 3.0, (
        f'reference={reference * 1000:.1f}ms fused={fused * 1000:.1f}ms')

    # 绝对断言：rank() 端到端单次 <100ms（132ms 基线的量级）
    t0 = time.perf_counter()
    ranked = emb.rank(store, query, MODEL, dim, limit=10)
    elapsed = time.perf_counter() - t0
    assert len(ranked) == 10
    assert elapsed < 0.100, f'rank() took {elapsed * 1000:.1f}ms'


# -- 4. rebuild 全覆盖 -------------------------------------------------------


def test_rebuild_covers_rows_beyond_max_rebuild_rows(store):
    total_rows = emb.MAX_REBUILD_ROWS + 200
    with store.database.transaction() as connection:
        for i in range(total_rows):
            store.insert_memory(connection, {
                'id': f'bulk-{i:05d}',
                'content': f'bulk memory {i} tail{i:05d}',
                'created_at': _stamp(i % 600),
                'updated_at': _stamp(i % 600),
                'subject_user_id': SUBJECT_USER_ID,
                'status': 'active',
            })

    batch_sizes = []

    def fake_embed(texts):
        batch_sizes.append(len(texts))
        return [[1.0, 0.5, 0.25, 0.125] for _ in texts]

    result = emb.rebuild(store, MODEL, DIM, fake_embed)
    assert result['error'] == ''
    assert result['indexed'] == total_rows
    assert result['total'] == total_rows
    assert result['skipped'] == 0
    assert sum(batch_sizes) == total_rows
    assert max(batch_sizes) <= emb.BATCH_SIZE
    stored = store.connection.execute(
        'SELECT COUNT(*) FROM memory_embeddings WHERE model=? AND dim=?',
        (MODEL, DIM)).fetchone()[0]
    assert stored == total_rows

    # 全部已嵌入后，再次 rebuild 没有剩余行可处理
    again = emb.rebuild(store, MODEL, DIM, fake_embed)
    assert again == {'indexed': 0, 'total': 0, 'skipped': 0, 'error': ''}


def test_rebuild_keeps_written_batches_and_resumes_after_embed_failure(store):
    total_rows = 200
    with store.database.transaction() as connection:
        for i in range(total_rows):
            store.insert_memory(connection, {
                'id': f'fail-{i:03d}',
                'content': f'failure memory {i}',
                'created_at': _stamp(i),
                'updated_at': _stamp(i),
                'subject_user_id': SUBJECT_USER_ID,
                'status': 'active',
            })

    calls = {'count': 0}

    class EmbedBoom(Exception):
        pass

    def exploding_embed(texts):
        calls['count'] += 1
        if calls['count'] >= 4:
            raise EmbedBoom('embedder exploded')
        return [[0.5, 0.5, 0.5, 0.5] for _ in texts]

    result = emb.rebuild(store, MODEL, DIM, exploding_embed)
    assert result['error'] == 'EmbedBoom'
    assert result['indexed'] == 3 * emb.BATCH_SIZE
    assert emb.last_error(store) == 'EmbedBoom'
    stored = store.connection.execute(
        'SELECT COUNT(*) FROM memory_embeddings WHERE model=? AND dim=?',
        (MODEL, DIM)).fetchone()[0]
    assert stored == 3 * emb.BATCH_SIZE

    # 续跑：已写入的批次被 LEFT JOIN 过滤，剩余行补齐后清掉错误
    resumed = emb.rebuild(store, MODEL, DIM,
                          lambda texts: [[0.25, 0.5, 0.75, 1.0] for _ in texts])
    assert resumed['error'] == ''
    assert resumed['indexed'] == total_rows - 3 * emb.BATCH_SIZE
    assert emb.last_error(store) == ''
    stored = store.connection.execute(
        'SELECT COUNT(*) FROM memory_embeddings WHERE model=? AND dim=?',
        (MODEL, DIM)).fetchone()[0]
    assert stored == total_rows


def test_rebuild_counts_empty_content_rows_as_skipped(store):
    _insert(store, 'skip-a', 'real content', created_at=_stamp(0))
    _insert(store, 'skip-b', '   ', created_at=_stamp(1))
    result = emb.rebuild(store, MODEL, DIM,
                         lambda texts: [[0.5, 0.5, 0.5, 0.5] for _ in texts])
    assert result['error'] == ''
    assert result['total'] == 2
    assert result['skipped'] == 1
    assert result['indexed'] == 1


# -- 5. 容错回归 --------------------------------------------------------------


def test_damaged_vector_missing_table_and_other_model_degrade_cleanly(store):
    # 空库与无该 model 的嵌入行都返回空
    assert emb.rank(store, _vec(1), MODEL, DIM, limit=5) == []
    _insert(store, 'tol-a', 'first memory', created_at=_stamp(0))
    emb.upsert(store, 'tol-a', 'other-model', DIM, _vec(1))
    assert emb.rank(store, _vec(1), MODEL, DIM, limit=5) == []

    # 损坏 blob 被跳过不崩，其余行仍可检索
    _insert(store, 'tol-b', 'second memory', created_at=_stamp(1))
    emb.upsert(store, 'tol-b', MODEL, DIM, _vec(2))
    _insert(store, 'tol-c', 'third memory', created_at=_stamp(2))
    emb.upsert(store, 'tol-c', MODEL, DIM, _vec(3))
    with store.database.transaction() as connection:
        connection.execute(
            'UPDATE memory_embeddings SET vector=? WHERE memory_id=?',
            (b'damaged', 'tol-b'))
    ranked = emb.rank(store, _vec(3), MODEL, DIM, limit=5)
    assert [item['id'] for item in ranked] == ['tol-c']

    # 表被删除：sqlite3.Error 容错返回空
    with store.database.transaction() as connection:
        connection.execute('DROP TABLE memory_embeddings')
    assert emb.rank(store, _vec(3), MODEL, DIM, limit=5) == []


def test_candidate_scan_truncation_records_error(store, monkeypatch):
    # 硬顶缩到 5 行：第 6 行会被截断并 record_error。
    # 全部行共用查询向量本身 → 每行得分都 >0，被扫到即被接受，
    # 计数不受 score>0 过滤干扰。
    monkeypatch.setattr(emb, 'MAX_CANDIDATE_SCAN_ROWS', 5)
    shared = _vec(1)
    for i in range(8):
        memory_id = f'trunc-{i}'
        _insert(store, memory_id, f'truncation memory {i}', created_at=_stamp(i))
        assert emb.upsert(store, memory_id, MODEL, DIM, shared)

    ranked = emb.rank(store, shared, MODEL, DIM, limit=10)
    assert len(ranked) == 5
    assert 'truncated at 5 rows' in emb.last_error(store)
    emb.clear_error(store)
    assert emb.last_error(store) == ''


def test_candidate_scan_stops_cleanly_when_exactly_at_cap(store, monkeypatch):
    # 恰好在硬顶处扫尽：不算截断，不记错误
    monkeypatch.setattr(emb, 'MAX_CANDIDATE_SCAN_ROWS', 6)
    shared = _vec(1)
    for i in range(6):
        memory_id = f'exact-{i}'
        _insert(store, memory_id, f'exact memory {i}', created_at=_stamp(i))
        assert emb.upsert(store, memory_id, MODEL, DIM, shared)

    ranked = emb.rank(store, shared, MODEL, DIM, limit=10)
    assert len(ranked) == 6
    assert emb.last_error(store) == ''
