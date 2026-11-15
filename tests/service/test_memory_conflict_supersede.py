"""chat 记忆矛盾自动 supersede：写路径新版本胜出 + detect 预取与自动裁决。

矛盾 chat 记忆不再靠双侧 disputed 封禁（检索硬过滤 status='active' 会把
两条一起藏掉）：写路径 propose 在同主题 key-field 冲突的旧 chat 行存在时
直接让新版 supersede 旧版；detect 对已共存的 chat 对按 created_at 自动
裁决，manual/protected 或无法定序的对维持双侧 disputed 的人工行为。
"""

import pathlib
import sqlite3
import time
import uuid

import pytest

from service.database import Database
from service.memory import conflicts, policy, search, writer
from service.memory.store import MemoryStore, utc_now


class _Environment:
    def __init__(self, tmp_path):
        self.path = pathlib.Path(tmp_path) / 'pet.db'
        self.database = Database(self.path)
        self.memory = MemoryStore(self.database, data_dir=tmp_path)
        self.database.memory_store_v3 = self.memory

    def close(self):
        self.database.close()


@pytest.fixture
def env(tmp_path):
    environment = _Environment(tmp_path)
    yield environment
    environment.close()


def _insert_chat(env, content, source_type='chat', protected=0, topic_id=None,
                 created_at=None):
    """直插一条 active 记忆（含正文与全文索引），时间可指定以便定序。"""
    memory_id = uuid.uuid4().hex
    now = created_at or utc_now()
    body_hash = env.memory.write_body(memory_id, content)
    with env.database.transaction() as connection:
        env.memory.insert_memory(connection, {
            'id': memory_id, 'content': content, 'created_at': now,
            'subject_user_id': 'owner', 'category': 'general',
            'importance': 0.5, 'source_type': source_type,
            'updated_at': now, 'status': 'active', 'body_hash': body_hash,
            'mention_count': 1, 'session_count': 1, 'recall_count': 0,
            'confidence': 0.5, 'strength': policy.strength(0.5),
            'last_reinforced_at': now,
            'dedupe_key': policy.normalize_text(content)[:200],
            'superseded_by': None, 'supersedes_id': None,
            'fuzz_level': 0, 'memory_level': 'short',
            'topic_id': topic_id, 'protected': protected,
        })
    env.memory.index_memory(memory_id, content)
    return memory_id


def _propose_chat(env, text):
    return writer.propose(env.memory, text, category='schedule',
                          source_type='chat')


def test_chat_conflict_write_supersedes_old_version(env):
    first = _propose_chat(env, '我下午3点有会')
    assert first['status'] == writer.COMMITTED
    second = _propose_chat(env, '我下午5点有会')
    assert second['status'] == writer.COMMITTED

    old_row = env.memory.get_memory(first['memory']['id'])
    new_row = env.memory.get_memory(second['memory']['id'])
    assert old_row['status'] == 'superseded'
    assert old_row['superseded_by'] == new_row['id']
    assert new_row['status'] == 'active'
    assert new_row['supersedes_id'] == old_row['id']

    payload = env.memory.latest_operation_payload(old_row['id'], 'supersede')
    assert payload['superseded_by'] == new_row['id']
    assert payload['reason'] == 'auto_conflict'

    # 修复前：两条共存，search 2 hits；修复后恰 1 hit 且是新版
    hits = search.search(env.memory, '下午有会', limit=5)
    assert [hit['id'] for hit in hits] == [new_row['id']]

    texts = search.search_for_context(env.database, '下午有会', {})
    injected = ''.join(texts)
    assert '5点' in injected
    assert '3点' not in injected


def test_third_variant_extends_supersede_chain(env):
    ids = []
    for text in ('我下午3点有会', '我下午5点有会', '我下午7点有会'):
        outcome = _propose_chat(env, text)
        assert outcome['status'] == writer.COMMITTED
        ids.append(outcome['memory']['id'])

    chain = env.memory.version_chain(ids[-1])
    assert [row['id'] for row in chain] == ids
    assert [env.memory.get_memory(item)['status'] for item in ids] == \
        ['superseded', 'superseded', 'active']
    assert env.memory.get_memory(ids[0])['superseded_by'] == ids[1]
    assert env.memory.get_memory(ids[1])['superseded_by'] == ids[2]
    hits = search.search(env.memory, '下午有会', limit=5)
    assert [hit['id'] for hit in hits] == [ids[-1]]


def test_manual_source_pair_keeps_both_and_disputes(env):
    first = writer.propose(env.memory, '我下午3点有会', category='schedule')
    second = writer.propose(env.memory, '我下午5点有会', category='schedule')
    assert first['status'] == writer.COMMITTED
    assert second['status'] == writer.COMMITTED
    first_id = first['memory']['id']
    second_id = second['memory']['id']

    # manual 来源矛盾不触发写路径自动 supersede，两条保持共存（旧行为）
    assert env.memory.get_memory(first_id)['status'] == 'active'
    assert env.memory.get_memory(second_id)['status'] == 'active'

    created = conflicts.detect(env.memory)
    assert len(created) == 1
    assert created[0]['status'] == conflicts.STATUS_OPEN
    assert env.memory.get_memory(first_id)['status'] == 'disputed'
    assert env.memory.get_memory(second_id)['status'] == 'disputed'


def test_manual_incoming_does_not_supersede_chat_row(env):
    first = _propose_chat(env, '我下午3点有会')
    second = writer.propose(env.memory, '我下午5点有会', category='schedule')
    assert second['status'] == writer.COMMITTED
    assert env.memory.get_memory(first['memory']['id'])['status'] == 'active'
    assert env.memory.get_memory(second['memory']['id'])['status'] == 'active'


def test_protected_old_chat_row_blocks_auto_supersede(env):
    protected_id = _insert_chat(env, '我下午3点有会', protected=1)
    outcome = _propose_chat(env, '我下午5点有会')
    assert outcome['status'] == writer.COMMITTED
    new_id = outcome['memory']['id']

    # protected 旧行不被自动 supersede，detect 落回双侧 disputed
    assert env.memory.get_memory(protected_id)['status'] == 'active'
    assert env.memory.get_memory(new_id)['status'] == 'active'
    created = conflicts.detect(env.memory)
    assert len(created) == 1
    assert created[0]['status'] == conflicts.STATUS_OPEN
    assert env.memory.get_memory(protected_id)['status'] == 'disputed'
    assert env.memory.get_memory(new_id)['status'] == 'disputed'


def test_detect_auto_resolves_chat_pair_and_keeps_manual_disputed(env):
    old_id = _insert_chat(env, '我下午3点有会',
                          created_at='2026-01-01T09:00:00.000000Z')
    new_id = _insert_chat(env, '我下午5点有会',
                          created_at='2026-01-02T09:00:00.000000Z')
    manual_a = _insert_chat(env, '猫的生日是3月5日', source_type='manual',
                            topic_id='cat-birthday',
                            created_at='2026-01-03T09:00:00.000000Z')
    manual_b = _insert_chat(env, '猫的生日是3月6日', source_type='manual',
                            topic_id='cat-birthday',
                            created_at='2026-01-04T09:00:00.000000Z')

    hits_before = search.search(env.memory, '下午有会', limit=5)
    assert len(hits_before) == 2

    created = conflicts.detect(env.memory)
    assert len(created) == 2
    assert {item['status'] for item in created} == \
        {conflicts.STATUS_OPEN, conflicts.STATUS_RESOLVED}

    resolved = next(item for item in created
                    if item['status'] == conflicts.STATUS_RESOLVED)
    assert resolved['superseded'] == old_id
    assert resolved['kept'] == new_id
    old_row = env.memory.get_memory(old_id)
    assert old_row['status'] == 'superseded'
    assert old_row['superseded_by'] == new_id
    assert env.memory.get_memory(new_id)['status'] == 'active'
    payload = env.memory.latest_operation_payload(old_id, 'supersede')
    assert payload['reason'] == 'auto_conflict'
    assert payload['superseded_by'] == new_id

    disputed = next(item for item in created
                    if item['status'] == conflicts.STATUS_OPEN)
    assert {disputed['memory_a'], disputed['memory_b']} == {manual_a, manual_b}
    assert env.memory.get_memory(manual_a)['status'] == 'disputed'
    assert env.memory.get_memory(manual_b)['status'] == 'disputed'

    listed = {item['id']: item['status']
              for item in conflicts.list_conflicts(env.memory)}
    assert listed[resolved['id']] == 'resolved'
    assert listed[disputed['id']] == 'open'

    hits_after = search.search(env.memory, '下午有会', limit=5)
    assert [hit['id'] for hit in hits_after] == [new_id]


def test_unorderable_chat_pair_stays_disputed(env):
    # created_at 完全相同无法定序：维持双侧 disputed，不自动裁决
    first_id = _insert_chat(env, '我下午3点有会',
                            created_at='2026-01-01T09:00:00.000000Z')
    second_id = _insert_chat(env, '我下午5点有会',
                             created_at='2026-01-01T09:00:00.000000Z')
    created = conflicts.detect(env.memory)
    assert len(created) == 1
    assert created[0]['status'] == conflicts.STATUS_OPEN
    assert env.memory.get_memory(first_id)['status'] == 'disputed'
    assert env.memory.get_memory(second_id)['status'] == 'disputed'


def test_body_write_failure_keeps_old_rows_active(env, monkeypatch):
    first = _propose_chat(env, '我下午3点有会')
    old_id = first['memory']['id']

    def fail_write(memory_id, text):
        raise OSError('disk full')

    monkeypatch.setattr(env.memory, 'write_body', fail_write)
    second = _propose_chat(env, '我下午5点有会')
    assert second['status'] == writer.QUEUED
    assert second['reason'] == 'body_write_failed'
    # 正文写失败走 QUEUED 兜底，旧行保持 active，等 outbox 后由 detect 收敛
    assert env.memory.get_memory(old_id)['status'] == 'active'


def test_supersede_transaction_failure_restores_old_row(env, monkeypatch):
    first = _propose_chat(env, '我下午3点有会')
    old_id = first['memory']['id']

    def fail_update(*args, **kwargs):
        raise sqlite3.OperationalError('metadata write failed')

    monkeypatch.setattr(env.memory, 'update_memory', fail_update)
    with pytest.raises(sqlite3.Error):
        _propose_chat(env, '我下午5点有会')

    # 事务回滚 + 正文清理：旧行原样保留，残留正文不多不少
    row = env.memory.get_memory(old_id)
    assert row['status'] == 'active'
    assert row['superseded_by'] is None
    remaining = env.database.connection.execute(
        "SELECT COUNT(*) FROM memories WHERE status='active'").fetchone()[0]
    assert remaining == 1
    assert len(list(env.memory.bodies_dir.iterdir())) == 1


def test_detect_on_200_active_rows_is_fast(env):
    now = utc_now()
    with env.database.transaction() as connection:
        for index in range(200):
            # 相邻话题（同主题候选、key-field 相同、互不冲突），
            # 覆盖 detect 的纯扫描路径；避开否定字（U+4E0D 是「不」）。
            content = f'独立话题{chr(0x4e10 + index)}的记忆正文'
            env.memory.insert_memory(connection, {
                'id': f'row{index:03d}', 'content': content,
                'created_at': now, 'subject_user_id': 'owner',
                'category': 'general', 'importance': 0.5,
                'source_type': 'chat', 'updated_at': now,
                'status': 'active', 'body_hash': '',
                'mention_count': 1, 'session_count': 1, 'recall_count': 0,
                'confidence': 0.5, 'strength': policy.strength(0.5),
                'last_reinforced_at': now,
                'dedupe_key': policy.normalize_text(content)[:200],
                'superseded_by': None, 'supersedes_id': None,
                'fuzz_level': 0, 'memory_level': 'short',
                'topic_id': None, 'protected': 0,
            })

    started = time.perf_counter()
    created = conflicts.detect(env.memory)
    elapsed = time.perf_counter() - started

    assert created == []
    assert elapsed < 0.2, f'detect() 耗时 {elapsed:.3f}s（200 行，阈值 0.2s）'
