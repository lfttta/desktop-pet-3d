"""Growth wiring: chat candidate categories, evidence and the runtime tick.

These tests cover the path from a structured chat memory candidate, through the
memory writer, into personality evidence, and the periodic scan that turns that
evidence into pending candidates.
"""

from datetime import datetime, timezone
import json

import pytest

from service.ai.orchestrator import ChatOrchestrator
from service.ai.schema import parse_structured
from service.database import Database
from service.features import growth as growth_feature
from service.memory.store import DEFAULT_CATEGORY, MemoryStore
from service.settings import SettingsStore
from service.state import growth as growth_state

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)


class FakeContext:
    """Minimal RequestContext: just a database, settings and tick registry."""

    def __init__(self, database, store):
        self.database = database
        self.store = store
        self.hooks = []

    def register_tick(self, hook):
        self.hooks.append(hook)


@pytest.fixture
def database(tmp_path):
    db = Database(tmp_path / 'pet.db')
    MemoryStore(db)
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def store(tmp_path):
    return SettingsStore(tmp_path)


def _enable(store, **overrides):
    config = {
        'enabled': True,
        'min_sessions': 1,
        'min_memories': 1,
        'max_step': 0.05,
        'cooldown_days': 0,
        'locked_traits': [],
    }
    config.update(overrides)
    store.update({'growth': config})
    return config


def _sessions(database, count=1):
    with database.transaction() as connection:
        existing = connection.execute(
            'SELECT COUNT(*) FROM conversations').fetchone()[0]
        for index in range(existing, existing + count):
            connection.execute(
                'INSERT INTO conversations(id,title,subject_user_id,created_at) '
                'VALUES (?,?,?,?)',
                (f'c{index}', 'chat', 'owner', NOW.isoformat()))


def _memory(database, memory_id, content, dedupe_key,
            category='preference', source_type='chat', source_message_id=None):
    with database.transaction() as connection:
        connection.execute(
            'INSERT INTO memories(id,content,created_at,subject_user_id,category,'
            'source_type,source_message_id,updated_at,dedupe_key,status) '
            'VALUES (?,?,?,?,?,?,?,?,?,?)',
            (memory_id, content, NOW.isoformat(), 'owner', category,
             source_type, source_message_id, NOW.isoformat(), dedupe_key,
             'active'))


def test_eligible_categories_and_corrections_become_evidence(database, store):
    config = _enable(store, min_memories=1)
    _sessions(database, 1)
    _memory(database, 'm1', '你让我觉得很温暖', 'event-1',
            category='preference')
    _memory(database, 'm2', '和你在一起更亲近了', 'event-2',
            category='relationship')
    _memory(database, 'm3', '和你一起玩很开心', 'event-3',
            category='feedback')
    _memory(database, 'm4', '你说话很直接', 'event-4',
            category='general', source_type='correction')
    _memory(database, 'm5', '你今天吃了面', 'event-5',
            category='general', source_type='chat')

    with database.transaction() as connection:
        evidence = growth_state._collect_evidence(connection)
        created = growth_state.sync_candidates(connection, config)

    warmth = set(evidence['warmth'].values())
    assert {'m1', 'm2'}.issubset(warmth)
    assert 'm3' in evidence['playfulness'].values()
    assert 'm4' in evidence['directness'].values()
    assert 'm5' not in warmth
    assert {candidate['trait'] for candidate in created} == {
        'warmth', 'playfulness', 'directness'}


def test_tick_generates_candidates_without_growth_list(database, store):
    _enable(store, min_memories=2)
    _sessions(database, 1)
    ctx = FakeContext(database, store)
    assert growth_feature.register_tick(ctx) is True
    assert len(ctx.hooks) == 1

    # Registering must not scan by itself; nothing exists yet.
    with database.transaction() as connection:
        assert growth_state.list_candidates(connection) == []

    _memory(database, 'm1', '你让我觉得很温暖', 'event-1')
    _memory(database, 'm2', '谢谢你的关心', 'event-2')

    # First runtime tick (runtime passes a monotonic float) scans immediately.
    ctx.hooks[0](100.0)
    with database.transaction() as connection:
        candidates = growth_state.list_candidates(connection)
    assert len(candidates) == 1
    assert candidates[0]['trait'] == 'warmth'
    assert candidates[0]['status'] == 'pending'


def test_tick_scans_at_most_every_ten_minutes(database, store, monkeypatch):
    _enable(store)
    _sessions(database, 1)
    created = []

    def fake_sync(connection, config):
        created.append(config)
        return []

    monkeypatch.setattr(growth_state, 'sync_candidates', fake_sync)
    ctx = FakeContext(database, store)
    assert growth_feature.register_tick(ctx) is True

    ctx.hooks[0](100.0)
    assert len(created) == 1
    ctx.hooks[0](100.0 + growth_feature.TICK_INTERVAL_SECONDS - 1)
    assert len(created) == 1
    ctx.hooks[0](100.0 + growth_feature.TICK_INTERVAL_SECONDS)
    assert len(created) == 2


def test_disabled_growth_tick_does_not_scan(database, store, monkeypatch):
    store.update({'growth': {'enabled': False}})
    created = []
    monkeypatch.setattr(growth_state, 'sync_candidates',
                        lambda connection, config: created.append(config))
    ctx = FakeContext(database, store)
    assert growth_feature.register_tick(ctx) is True

    ctx.hooks[0](100.0)
    ctx.hooks[0](100.0 + growth_feature.TICK_INTERVAL_SECONDS)
    assert created == []


def test_orchestrator_passes_structured_candidate_metadata(database, store):
    store.update({'memory': {'auto_capture': True}})
    orchestrator = ChatOrchestrator(store, database)
    memory_settings = store.settings['memory']

    ops = orchestrator._memory_ops([
        {'text': '你让我觉得很温暖', 'category': 'preference',
         'importance': 0.9, 'source_type': 'chat'},
        {'text': '谢谢你的关心', 'category': 'preference',
         'importance': 0.8, 'source_type': 'chat'},
    ], memory_settings)

    assert [op['status'] for op in ops] == ['committed', 'committed']
    assert ops[0]['category'] == 'preference'
    assert ops[0]['importance'] == pytest.approx(0.9)
    rows = database.connection.execute(
        'SELECT category, importance, source_type FROM memories ORDER BY rowid'
    ).fetchall()
    assert rows == [('preference', pytest.approx(0.9), 'chat'),
                    ('preference', pytest.approx(0.8), 'chat')]

    # The stored category is what the growth evidence collector reads.
    _enable(store, min_memories=2)
    _sessions(database, 1)
    with database.transaction() as connection:
        candidates = growth_state.sync_candidates(
            connection, store.settings['growth'])
    assert len(candidates) == 1
    assert candidates[0]['trait'] == 'warmth'
    assert len(candidates[0]['evidence_ids']) == 2


def test_parse_structured_candidates_become_growth_evidence(database, store):
    """The real model reply shape reaches the writer and the evidence scan."""
    store.update({'memory': {'auto_capture': True}})
    orchestrator = ChatOrchestrator(store, database)
    reply = {
        'text': '我记住了。',
        'speak': '',
        'action': 'none',
        'memory_candidates': [
            {'text': '你让我觉得很温暖', 'category': 'preference',
             'importance': 0.9, 'source_type': 'chat'},
            {'text': '谢谢你的关心', 'category': 'preference',
             'importance': 0.8, 'source_type': 'chat'},
        ],
        'state_suggestion': {},
        'confidence': 0.9,
    }

    structured = parse_structured(json.dumps(reply, ensure_ascii=False))
    assert structured['memory_candidates'][0]['category'] == 'preference'
    ops = orchestrator._memory_ops(
        structured['memory_candidates'], store.settings['memory'])

    assert [op['status'] for op in ops] == ['committed', 'committed']
    _enable(store, min_memories=2)
    _sessions(database, 1)
    with database.transaction() as connection:
        candidates = growth_state.sync_candidates(
            connection, store.settings['growth'])
    assert len(candidates) == 1
    assert candidates[0]['trait'] == 'warmth'
    assert len(candidates[0]['evidence_ids']) == 2


def test_plain_string_candidates_keep_the_text_only_shape(database, store):
    orchestrator = ChatOrchestrator(store, database)
    ops = orchestrator._memory_ops(['喜欢下雨天'], {'enabled': True})
    assert ops == [{'operation': 'propose', 'status': 'pending',
                    'content': '喜欢下雨天'}]
    assert DEFAULT_CATEGORY == 'general'
