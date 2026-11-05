"""Personality growth contracts: evidence, approval, history and rollback."""

from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest

from service.database import Database
from service.features import growth as growth_feature
from service.features import state as state_feature
from service.memory.store import MemoryStore
from service.protocol import ProtocolError
from service.settings import SettingsStore
from service.state import character, growth as growth_state

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def database(tmp_path):
    db = Database(tmp_path / 'pet.db')
    # Add the memory extras (status, dedupe_key, ...) the evidence query uses.
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
        'min_sessions': 3,
        'min_memories': 2,
        'max_step': 0.05,
        'cooldown_days': 0,
        'locked_traits': [],
    }
    config.update(overrides)
    store.update({'growth': config})
    return config


def _sessions(database, count=3):
    with database.transaction() as connection:
        existing = connection.execute(
            'SELECT COUNT(*) FROM conversations').fetchone()[0]
        for index in range(existing, existing + count):
            connection.execute(
                'INSERT INTO conversations(id,title,subject_user_id,created_at) '
                'VALUES (?,?,?,?)',
                (f'c{index}', 'chat', 'owner', NOW.isoformat()))


def _memory(database, memory_id, content, dedupe_key,
            category='preference', source_message_id=None):
    with database.transaction() as connection:
        connection.execute(
            'INSERT INTO memories(id,content,created_at,subject_user_id,category,'
            'source_type,source_message_id,updated_at,dedupe_key) '
            'VALUES (?,?,?,?,?,?,?,?,?)',
            (memory_id, content, NOW.isoformat(), 'owner', category,
             'chat', source_message_id, NOW.isoformat(), dedupe_key))


def _sync(database, config, now=NOW):
    with database.transaction() as connection:
        return growth_state.sync_candidates(connection, config, now=now)


def _list(store, database, request_id='list'):
    return growth_feature.list_growth({'id': request_id, 'payload': {}}, store,
                                      SimpleNamespace(database=database))


def _state(store, database):
    return state_feature.get({'id': 'state', 'payload': {}}, store,
                             SimpleNamespace(database=database))[1]


def _approve(store, database, candidate_id):
    return growth_feature.approve(
        {'id': 'approve', 'payload': {'candidate_id': candidate_id}}, store,
        SimpleNamespace(database=database))[1]


def _lock(store, database, trait, locked):
    return growth_feature.personality_lock(
        {'id': 'lock', 'payload': {'trait': trait, 'locked': locked}},
        store, SimpleNamespace(database=database))[1]


def test_core_traits_always_follow_the_character_card(database):
    payload = json.dumps({
        'pad': {},
        'psych': {},
        'personality': {'traits': {'kindness': 0.0, 'warmth': 0.9}},
        'updated_at': NOW.isoformat(),
    })
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO character_state(subject_user_id,payload,updated_at) "
            "VALUES ('owner',?,?)", (payload, NOW.isoformat()))
    with database.transaction() as connection:
        state = character.load_state(connection, now=NOW, apply_decay=False)
    assert state['personality']['kindness'] == (
        character.CHARACTER_CARD['personality']['kindness'])
    assert state['personality']['warmth'] == pytest.approx(0.9)
    assert character.personality_values(state)['kindness'] == (
        character.CHARACTER_CARD['personality']['kindness'])


def test_same_event_evidence_is_deduplicated(database, store):
    _enable(store)
    _sessions(database, 3)
    _memory(database, 'm1', 'you are so warm', 'event-1')
    _memory(database, 'm2', 'thanks for caring about me', 'event-1')

    _, payload = _list(store, database)
    assert payload['candidates'] == []


def test_same_source_message_is_one_event(database, store):
    _enable(store)
    _sessions(database, 3)
    _memory(database, 'm1', 'you are so warm', 'event-1',
            source_message_id='msg-1')
    _memory(database, 'm2', 'thanks for caring about me', 'event-2',
            source_message_id='msg-1')

    _, payload = _list(store, database)
    assert payload['candidates'] == []


def test_threshold_generates_a_capped_candidate(database, store):
    config = _enable(store, max_step=0.05)
    _sessions(database, 3)
    _memory(database, 'm1', 'you are so warm', 'event-1')
    _memory(database, 'm2', 'thanks for caring about me', 'event-2')

    _, payload = _list(store, database)
    assert len(payload['candidates']) == 1
    candidate = payload['candidates'][0]
    assert candidate['trait'] == 'warmth'
    assert candidate['status'] == 'pending'
    assert len(candidate['evidence_ids']) == 2
    assert 0 < candidate['delta'] <= config['max_step']
    assert candidate['delta'] == pytest.approx(0.04)
    assert growth_feature.CAPABILITY['available'] is True


def test_session_threshold_is_required(database, store):
    _enable(store)
    _sessions(database, 2)
    for index in range(4):
        _memory(database, f'm{index}', 'you are so warm', f'event-{index}')
    _, payload = _list(store, database)
    assert payload['candidates'] == []

    # The third session reaches the threshold for the same evidence.
    _sessions(database, 1)
    _, payload = _list(store, database)
    assert len(payload['candidates']) == 1
    assert payload['candidates'][0]['trait'] == 'warmth'


def test_approve_applies_value_and_records_history(database, store):
    _enable(store)
    _sessions(database, 3)
    _memory(database, 'm1', 'you are so warm', 'event-1')
    _memory(database, 'm2', 'thanks for caring about me', 'event-2')
    candidate = _list(store, database)[1]['candidates'][0]

    outcome = _approve(store, database, candidate['id'])
    assert outcome['status'] == 'applied'
    assert outcome['trait'] == 'warmth'
    assert outcome['old_value'] == character.CHARACTER_CARD['personality']['warmth']
    assert outcome['new_value'] == pytest.approx(outcome['old_value'] + candidate['delta'])
    assert outcome['history_id']

    view = _state(store, database)
    assert view['personality']['warmth'] == pytest.approx(outcome['new_value'])
    assert set(view['locked']) == {'kindness', 'independence'}

    with database.transaction() as connection:
        stored = character.load_state(connection, now=NOW, apply_decay=False)
    assert stored['personality']['warmth'] == pytest.approx(outcome['new_value'])
    assert stored['personality']['traits']['warmth'] == pytest.approx(
        outcome['new_value'])

    _, history = growth_feature.growth_history(
        {'id': 'history', 'payload': {}}, store, SimpleNamespace(database=database))
    assert len(history['history']) == 1
    entry = history['history'][0]
    assert entry['id'] == outcome['history_id']
    assert entry['trait'] == 'warmth'
    assert entry['old_value'] == outcome['old_value']
    assert entry['new_value'] == outcome['new_value']
    assert entry['reverted_at'] is None

    _, listed = _list(store, database)
    assert listed['candidates'] == []
    with database.transaction() as connection:
        row = connection.execute(
            'SELECT status,decision,applied_at FROM growth_candidates WHERE id=?',
            (candidate['id'],)).fetchone()
    assert row[0] == 'applied' and row[1] == 'approved' and row[2]

    with pytest.raises(ProtocolError) as caught:
        _approve(store, database, candidate['id'])
    assert caught.value.code == 'invalid_state'

    # A later model delta must not reset the approved value.
    with database.transaction() as connection:
        character.apply_delta(connection, {
            'pad': {'pleasure': 0.2},
            'personality': {'traits': {'warmth': 0.0}},
        }, now=NOW)
    assert _state(store, database)['personality']['warmth'] == pytest.approx(
        outcome['new_value'])


def test_rollback_restores_the_old_value(database, store):
    _enable(store)
    _sessions(database, 3)
    _memory(database, 'm1', 'you are so warm', 'event-1')
    _memory(database, 'm2', 'thanks for caring about me', 'event-2')
    candidate = _list(store, database)[1]['candidates'][0]
    applied = _approve(store, database, candidate['id'])

    result = growth_feature.rollback(
        {'id': 'rollback', 'payload': {'history_id': applied['history_id']}},
        store, SimpleNamespace(database=database))[1]
    assert result == {'status': 'reverted', 'trait': 'warmth',
                      'value': applied['old_value']}
    assert _state(store, database)['personality']['warmth'] == applied['old_value']

    _, history = growth_feature.growth_history(
        {'id': 'history', 'payload': {}}, store, SimpleNamespace(database=database))
    assert history['history'][0]['reverted_at']

    with pytest.raises(ProtocolError) as caught:
        growth_feature.rollback(
            {'id': 'again', 'payload': {'history_id': applied['history_id']}},
            store, SimpleNamespace(database=database))
    assert caught.value.code == 'invalid_state'


def test_locked_trait_skips_generation_and_approval(database, store):
    _enable(store)
    _sessions(database, 3)
    _memory(database, 'm1', 'you are so warm', 'event-1')
    _memory(database, 'm2', 'thanks for caring about me', 'event-2')
    candidate = _list(store, database)[1]['candidates'][0]

    assert _lock(store, database, 'warmth', True) == {
        'trait': 'warmth', 'locked': True}
    assert 'warmth' in store.settings['growth']['locked_traits']
    assert 'warmth' in _state(store, database)['locked']

    with pytest.raises(ProtocolError) as caught:
        _approve(store, database, candidate['id'])
    assert caught.value.code == 'locked_trait'

    # Unlocking allows the still-pending candidate to be approved.
    _lock(store, database, 'warmth', False)
    assert 'warmth' not in store.settings['growth']['locked_traits']
    assert _approve(store, database, candidate['id'])['status'] == 'applied'

    _, listed = _list(store, database)
    assert listed['candidates'] == []


def test_locked_trait_never_generates_a_candidate(database, store):
    _enable(store, locked_traits=['curiosity'])
    _sessions(database, 3)
    _memory(database, 'm1', 'i am curious why you like this', 'event-1')
    _memory(database, 'm2', 'we should explore this question', 'event-2')

    _, payload = _list(store, database)
    assert payload['candidates'] == []
    assert _lock(store, database, 'curiosity', True)['locked'] is True
    assert 'curiosity' in _state(store, database)['locked']


def test_core_traits_cannot_be_unlocked(database, store):
    _enable(store)
    core = _lock(store, database, 'kindness', False)
    assert core == {'trait': 'kindness', 'locked': True}
    assert 'kindness' not in store.settings['growth']['locked_traits']
    assert set(_state(store, database)['locked']) == {'kindness', 'independence'}


def test_step_is_capped_and_cooldown_blocks_same_trait(database, store):
    config = _enable(store, max_step=0.02, cooldown_days=7)
    _sessions(database, 3)
    for index in range(5):
        _memory(database, f'm{index}', 'you are so warm', f'event-{index}')

    created = _sync(database, config, now=NOW)
    assert len(created) == 1
    assert created[0]['trait'] == 'warmth'
    assert created[0]['delta'] == pytest.approx(0.02)
    assert created[0]['delta'] <= config['max_step']

    with database.transaction() as connection:
        applied = growth_state.approve(connection, created[0]['id'], config, now=NOW)
    assert applied['new_value'] - applied['old_value'] == pytest.approx(0.02)

    _memory(database, 'm-new', 'thanks for caring about me', 'event-new')
    assert _sync(database, config, now=NOW + timedelta(days=6)) == []
    later = _sync(database, config, now=NOW + timedelta(days=8))
    assert len(later) == 1 and later[0]['trait'] == 'warmth'


def test_rejected_candidate_is_not_recreated_for_the_same_evidence(database, store):
    config = _enable(store)
    _sessions(database, 3)
    _memory(database, 'm1', 'you are so warm', 'event-1')
    _memory(database, 'm2', 'thanks for caring about me', 'event-2')
    candidate = _list(store, database)[1]['candidates'][0]

    rejected = growth_feature.reject(
        {'id': 'reject', 'payload': {'candidate_id': candidate['id']}},
        store, SimpleNamespace(database=database))[1]
    assert rejected == {'status': 'rejected', 'candidate_id': candidate['id']}
    assert _list(store, database)[1]['candidates'] == []
    assert _sync(database, config, now=NOW) == []

    _memory(database, 'm3', 'i love that you are warm', 'event-3')
    assert len(_sync(database, config, now=NOW)) == 1
