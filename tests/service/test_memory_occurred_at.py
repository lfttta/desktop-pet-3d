"""occurred_at 事实发生时间：附加列迁移、写入回填、排序回退与过期降权。

双时态最小子集：每条记忆区分世界时间线（occurred_at，写入时从原文确定性
解析）与系统时间线（created_at/updated_at）。检索 recency 优先按事实时间
轴衰减；archive 只对「写入时指向未来、且该时刻已过去超窗」的行降权。
"""

from datetime import datetime, timedelta, timezone

import pytest

from service.database import SCHEMA_VERSION, Database
from service.memory import jobs, policy, writer
from service.memory.search import search_for_context
from service.memory.store import MemoryStore

# 固定锚：2026-11-15 是周日，方便断言「下周三」等相对表达。
ANCHOR = '2026-11-15T08:30:00.000000Z'


def _stamp(days_ago=0.0, base=None):
    moment = (base if base is not None else datetime.now(timezone.utc)) \
        - timedelta(days=days_ago)
    return moment.strftime('%Y-%m-%dT%H:%M:%S.%fZ')


def _insert(store, memory_id, content, **overrides):
    fields = {
        'id': memory_id,
        'content': content,
        'created_at': _stamp(),
        'subject_user_id': 'owner',
        'category': 'general',
        'importance': 0.5,
        'source_type': 'manual',
        'updated_at': _stamp(),
        'status': 'active',
        'body_hash': '',
        'mention_count': 1,
        'session_count': 1,
        'recall_count': 0,
        'confidence': 0.5,
        'last_reinforced_at': _stamp(),
        'dedupe_key': content,
    }
    fields.update(overrides)
    fields['strength'] = policy.strength(
        fields['importance'], fields['mention_count'], fields['session_count'],
        fields['recall_count'], fields['confidence'])
    with store.database.transaction() as connection:
        store.insert_memory(connection, fields)


@pytest.fixture
def ranking_env(tmp_path):
    # 关闭 FTS：bm25 的位置相关度（1/(1+位次)）会淹没 recency 的 0.10 权重
    # 差；LIKE 路径让同文记忆的相关度完全同分，recency 才能单独裁决。
    database = Database(tmp_path / 'pet.db', enable_fts=False)
    store = MemoryStore(database)
    database.memory_store_v3 = store
    try:
        yield database, store
    finally:
        database.close()


# -- 迁移 -----------------------------------------------------------------


def test_occurred_at_migration_is_additive(tmp_path):
    path = tmp_path / 'pet.db'
    database = Database(path)  # 基础表：还没有 occurred_at 附加列
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO memories(id,content,created_at) "
            "VALUES ('legacy-1','旧记忆','2026-01-01T00:00:00Z')")
    database.close()

    reopened = Database(path)
    store = MemoryStore(reopened)  # store 自有迁移：ALTER TABLE ADD COLUMN
    try:
        columns = {row[1] for row in reopened.connection.execute(
            'PRAGMA table_info(memories)')}
        assert 'occurred_at' in columns
        # 附加列走 store 自有迁移，schema version 不变。
        assert reopened.connection.execute(
            'PRAGMA user_version').fetchone() == (SCHEMA_VERSION,)
        row = store.get_memory('legacy-1')
        assert row['content'] == '旧记忆'
        assert row['created_at'] == '2026-01-01T00:00:00Z'
        assert row['occurred_at'] is None  # 存量行为 NULL，读取侧走回退
    finally:
        reopened.close()


# -- 写入回填 ---------------------------------------------------------------


def test_propose_backfills_occurred_at(tmp_path):
    database = Database(tmp_path / 'pet.db')
    store = MemoryStore(database)
    try:
        outcome = writer.propose(store, '我2026年11月20日下午3点有会',
                                 source_type='chat')
        assert outcome['status'] == writer.COMMITTED
        row = store.get_memory(outcome['memory']['id'])
        assert row['occurred_at'] == '2026-11-20T15:00:00.000000Z'

        # 无时间 token：解析不出就存 NULL（fail-open），读取侧回退
        # updated_at——recency 仍随最后提起衰减，与旧行为等价。
        plain = writer.propose(store, '我喜欢吃苹果')
        plain_row = store.get_memory(plain['memory']['id'])
        assert plain_row['occurred_at'] is None
    finally:
        database.close()


# -- 纯函数 -----------------------------------------------------------------


@pytest.mark.parametrize(('text', 'expected'), [
    ('今天', '2026-11-15T00:00:00.000000Z'),
    ('明天', '2026-11-16T00:00:00.000000Z'),
    ('明日出行', '2026-11-16T00:00:00.000000Z'),
    ('3天后', '2026-11-18T00:00:00.000000Z'),
    ('下周三', '2026-11-18T00:00:00.000000Z'),
    ('下周日', '2026-11-22T00:00:00.000000Z'),
    ('14:30', '2026-11-15T14:30:00.000000Z'),
    ('8:30', '2026-11-15T08:30:00.000000Z'),
    ('下午3点', '2026-11-15T15:00:00.000000Z'),
    ('凌晨3点', '2026-11-15T03:00:00.000000Z'),
    ('晚上8点30分', '2026-11-15T20:30:00.000000Z'),
    ('15点30分', '2026-11-15T15:30:00.000000Z'),
    ('2026-11-20', '2026-11-20T00:00:00.000000Z'),
    ('2026年11月20日', '2026-11-20T00:00:00.000000Z'),
    ('11月20日', '2026-11-20T00:00:00.000000Z'),
    ('明天下午3点', '2026-11-16T15:00:00.000000Z'),
    ('我2026年11月20日下午3点有会', '2026-11-20T15:00:00.000000Z'),
    ('我喜欢吃苹果', None),
    ('去年冬天很冷', None),
    ('大后天见', None),
    ('前天去爬山', None),
    ('1000天后', None),       # 尾随「0天后」不允许命中
    ('9255-09-27', None),     # 年份远离锚点：URL/编号碎片，放弃
    ('2026-11-201', None),    # 日子粘着更长数字串，放弃
    ('', None),
])
def test_occurred_at_for_table(text, expected):
    assert policy.occurred_at_for(text, ANCHOR) == expected


def test_occurred_at_for_requires_usable_anchor():
    assert policy.occurred_at_for('明天', 'not-a-time') is None
    assert policy.occurred_at_for('明天', None) is None


# -- 检索排序 ----------------------------------------------------------------


def test_stale_fact_ranks_below_fresh_same_topic(ranking_env):
    database, store = ranking_env
    now = datetime.now(timezone.utc)
    # 「我下周三去上海」写于 60 天前：事实时刻早已过去，即使最近又被提起
    # （updated_at 崭新、mention 更多），也应排在同时写下的事实之后。
    _insert(store, 'fact-old', '我下周三去上海',
            created_at=_stamp(60, now), updated_at=_stamp(0, now),
            occurred_at=_stamp(60, now), mention_count=3,
            last_reinforced_at=_stamp(0, now))
    _insert(store, 'fact-new', '我下周三去上海',
            created_at=_stamp(0, now), updated_at=_stamp(0, now),
            occurred_at=_stamp(0, now))

    texts, ids = search_for_context(database, '下周三去上海', {}, 5, 500,
                                    with_ids=True)

    assert texts == ['我下周三去上海', '我下周三去上海']
    assert ids == ['fact-new', 'fact-old']


def test_null_occurred_at_keeps_updated_at_ordering(ranking_env):
    database, store = ranking_env
    # 存量行形态：occurred_at 为 NULL（未回填）。recency 回退 updated_at，
    # 两行同样新鲜，次序由 mention/strength 裁决——与改动前的行为一致。
    _insert(store, 'fact-old', '我下周三去上海', occurred_at=None,
            mention_count=3)
    _insert(store, 'fact-new', '我下周三去上海')

    texts, ids = search_for_context(database, '下周三去上海', {}, 5, 500,
                                    with_ids=True)

    assert ids == ['fact-old', 'fact-new']


def test_rementioned_memory_without_time_token_keeps_updated_at_recency(ranking_env):
    database, store = ranking_env
    now = datetime.now(timezone.utc)
    # 无时间 token 的记忆 30 天前写入、今天再次提起（merge 只更新
    # updated_at，不写 occurred_at）：recency 必须按「最后提起」算。
    # 若写入侧把 created_at 落进 occurred_at，读取侧 occurred_at 优先会让
    # 它按「首次写入」衰减、被更新更早但更强(mention=2)的静态行反超——
    # 本用例钉住该回归（静态行 score≈0.593 vs 正确 0.678 / 出错 0.583）。
    _insert(store, 'mem-merged', '我喜欢吃苹果',
            created_at=_stamp(30, now), updated_at=_stamp(0, now),
            last_reinforced_at=_stamp(0, now))
    _insert(store, 'mem-static', '我喜欢吃苹果',
            created_at=_stamp(30, now), updated_at=_stamp(30, now),
            mention_count=2)

    texts, ids = search_for_context(database, '喜欢吃苹果', {}, 5, 500,
                                    with_ids=True)

    assert texts == ['我喜欢吃苹果', '我喜欢吃苹果']
    assert ids == ['mem-merged', 'mem-static']


# -- 过期事实降权 -------------------------------------------------------------


def test_future_pointing_fact_gets_archived_after_window(tmp_path):
    database = Database(tmp_path / 'pet.db')
    store = MemoryStore(database)
    try:
        now = datetime.now(timezone.utc)
        # 写于 90 天前、指向 60 天后（即 30 天前）的时刻：事件已过去 30 天，
        # 超过 14 天窗口，即使 updated_at 一天前刚被提起也视为过期。
        _insert(store, 'fact-future', '我下周三去上海',
                created_at=_stamp(90, now), updated_at=_stamp(1, now),
                occurred_at=_stamp(30, now))
        # 过去时事实（直接插入的守卫单测：occurred_at < created_at；真实
        # propose 写路径见 test_past_time_fact_via_propose_not_archived）。
        _insert(store, 'fact-past', '我2024年在北京工作',
                created_at=_stamp(90, now), updated_at=_stamp(1, now),
                occurred_at='2024-01-01T00:00:00.000000Z')
        settings = {'memory': {'archive': {'enabled': True,
                                           'topic_stale_days': 14}}}

        queued = jobs._scan(store, settings, now)

        assert queued == 1
        keys = [jobs._payload(job).get('topic_key')
                for job in jobs.list_jobs(store, status='pending')]
        assert keys == [policy.topic_key('我下周三去上海')]

        # 归档任务真正执行后：未来时事实被归档，过去时事实保持 active。
        assert jobs.process(store, settings, now)[0]['result'] == {
            'status': 'archived', 'archived': 1}
        assert store.get_memory('fact-future')['status'] == 'archived'
        assert store.get_memory('fact-past')['status'] == 'active'
    finally:
        database.close()


def test_past_time_fact_via_propose_not_archived(tmp_path):
    database = Database(tmp_path / 'pet.db')
    store = MemoryStore(database)
    try:
        now = datetime.now(timezone.utc)
        outcome = writer.propose(store, '我2024年5月20日搬到了北京')
        assert outcome['status'] == writer.COMMITTED
        row = store.get_memory(outcome['memory']['id'])
        # 真实写路径产出的过去时事实：解析出的时刻早于写入时刻。
        assert row['occurred_at'] == '2024-05-20T00:00:00.000000Z'
        assert row['occurred_at'] < row['created_at']

        # occurred_at 过期判定只针对未来时事实，该行不受影响、不入队。
        queued = jobs._scan(store, {'memory': {'archive': {
            'enabled': True, 'topic_stale_days': 14}}}, now)
        assert queued == 0
        assert jobs.list_jobs(store, status='pending') == []
    finally:
        database.close()
