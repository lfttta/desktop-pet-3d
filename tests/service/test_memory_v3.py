"""Memory V3 minimal loop: isolation, corrections, body failures, recovery, FTS."""

import pathlib
import uuid

import pytest

from service.commands import dispatch
from service.database import Database
from service.memory import policy, search as memory_search, writer
from service.memory.store import MemoryStore, utc_now
from service.protocol import ProtocolError
from service.settings import SettingsStore


class _Context:
    def __init__(self, database):
        self.database = database
        self.frames = []
        self.ticks = []

    def emit(self, event, payload):
        self.frames.append((event, payload))

    def register_tick(self, hook):
        self.ticks.append(hook)


class _Environment:
    def __init__(self, tmp_path):
        self.path = pathlib.Path(tmp_path) / 'pet.db'
        self.database = Database(self.path)
        self.settings = SettingsStore(tmp_path)
        self.memory = MemoryStore(self.database)
        self.database.memory_store_v3 = self.memory
        self.context = _Context(self.database)

    def dispatch(self, command, payload):
        return dispatch({'version': 1, 'id': 'req-memory', 'type': command,
                         'payload': payload}, self.settings, self.context)

    def close(self):
        self.database.close()


@pytest.fixture
def env(tmp_path):
    environment = _Environment(tmp_path)
    yield environment
    environment.close()


def _direct_insert(env, subject_user_id, content):
    memory_id = uuid.uuid4().hex
    now = utc_now()
    with env.database.transaction() as connection:
        env.memory.insert_memory(connection, {
            'id': memory_id, 'content': content, 'created_at': now,
            'subject_user_id': subject_user_id, 'category': 'general',
            'importance': 0.5, 'source_type': 'manual',
            'updated_at': now, 'status': 'active', 'body_hash': '',
            'mention_count': 1, 'session_count': 1, 'recall_count': 0,
            'confidence': 0.5, 'strength': policy.strength(0.5),
            'last_reinforced_at': now,
            'dedupe_key': policy.normalize_text(content)[:200],
        })
    return memory_id


def _propose(env, text):
    event, payload = env.dispatch('memory.propose', {'text': text})
    assert event == 'result'
    return payload


def _search(env, query, limit=None):
    body = {'query': query}
    if limit is not None:
        body['limit'] = limit
    event, payload = env.dispatch('memory.search', body)
    assert event == 'result'
    return payload


def test_subject_filter_hides_other_users_memories(env):
    private_id = _direct_insert(env, 'other-user', 'rainy day plan secretive marker')
    owner_id = _propose(env, 'rainy day plan belongs to owner')['memory']['id']

    other = _search(env, 'rainy')
    ids = [item['id'] for item in other['results']]
    assert owner_id in ids
    assert private_id not in ids
    assert all(item['id'] != private_id for item in other['results'])

    hidden = _search(env, 'secretive')
    assert hidden['count'] == 0
    assert hidden['results'] == []
    # The search helper applies the same subject filter outside the command layer.
    assert private_id not in [item['id'] for item in memory_search.search(env.memory, 'rainy')]


def test_correction_supersedes_old_version_in_search(env):
    old = _propose(env, '我的猫叫小白')['memory']
    event, corrected = env.dispatch('memory.correct',
                                    {'memory_id': old['id'], 'text': '我的猫叫大白'})
    assert event == 'result'
    assert corrected['status'] == writer.SUPERSEDED
    assert corrected['previous_memory_id'] == old['id']
    new_id = corrected['memory']['id']

    rows = _search(env, '猫')['results']
    assert rows and rows[0]['id'] == new_id
    assert old['id'] not in [item['id'] for item in rows]
    assert env.memory.get_memory(old['id'])['status'] == 'superseded'


def test_body_write_failure_leaves_original_untouched_then_recovers(env, monkeypatch):
    old = _propose(env, '我的猫叫小白')['memory']
    body_before = env.memory.read_body(old['id'])
    hash_before = env.memory.get_memory(old['id'])['body_hash']

    def fail_write(memory_id, text):
        raise OSError('disk unavailable')

    with monkeypatch.context() as patch:
        patch.setattr(env.memory, 'write_body', fail_write)
        event, outcome = env.dispatch('memory.correct',
                                      {'memory_id': old['id'], 'text': '我的猫叫大白'})
    assert event == 'result'
    assert outcome['status'] == writer.QUEUED
    assert outcome['previous_memory_id'] == old['id']

    row = env.memory.get_memory(old['id'])
    assert row['status'] == 'active'
    assert row['body_hash'] == hash_before
    assert env.memory.read_body(old['id']) == body_before
    assert [item['id'] for item in _search(env, '猫')['results']] == [old['id']]

    processed = writer.process_outbox(env.memory)
    assert [item['status'] for item in processed] == [writer.SUPERSEDED]

    old_row = env.memory.get_memory(old['id'])
    assert old_row['status'] == 'superseded'
    rows = _search(env, '猫')['results']
    assert len(rows) == 1 and rows[0]['id'] == old_row['superseded_by']
    assert old_row['superseded_by'] in [item['id'] for item in rows]


def test_interrupted_delete_is_completed_from_outbox_after_reopen(env):
    memory = _propose(env, 'temporary fact to delete')['memory']
    body_path = env.memory.body_path(memory['id'])
    assert body_path.is_file()

    event, outcome = env.dispatch('memory.delete', {'memory_id': memory['id']})
    assert event == 'result'
    assert outcome['status'] == writer.QUEUED
    assert env.memory.get_memory(memory['id'])['status'] == 'deleting'
    assert body_path.is_file()
    pending = env.memory.pending_outbox()
    assert [(item['operation'], item['delivered_at']) for item in pending] == \
        [('delete_body', None)]

    path = env.path
    env.database.close()
    database = Database(path)
    restored = MemoryStore(database)
    try:
        processed = writer.process_outbox(restored)
        assert [item['status'] for item in processed] == ['deleted']
        assert restored.get_memory(memory['id'])['status'] == 'deleted'
        assert not restored.body_path(memory['id']).exists()
        assert memory_search.search(restored, 'temporary') == []
        assert restored.pending_outbox() == []
        # A second drain is idempotent.
        assert writer.process_outbox(restored) == []
    finally:
        database.close()


def test_corrupt_fts_index_is_rebuilt_from_memories(env):
    if not env.memory.fts_enabled:
        pytest.skip('FTS5 is not available in this SQLite build')
    first = _propose(env, '周末想去公园散步')['memory']
    second = _propose(env, '晚上读一本小说')['memory']
    assert env.database.connection.execute(
        'SELECT COUNT(*) FROM memory_fts').fetchone()[0] == 2

    with env.database.transaction() as connection:
        connection.execute('DROP TABLE memory_fts')

    # Missing index degrades to LIKE without losing results.
    degraded = {item['id'] for item in _search(env, '公园')['results']}
    assert degraded == {first['id']}

    assert env.memory.rebuild_index() == 2
    assert env.database.connection.execute(
        'SELECT COUNT(*) FROM memory_fts').fetchone()[0] == 2
    rebuilt = {item['id'] for item in _search(env, '小说')['results']}
    assert rebuilt == {second['id']}
    indexed = {row[0] for row in env.database.connection.execute(
        'SELECT memory_id FROM memory_fts')}
    assert indexed == {first['id'], second['id']}


def test_write_outcomes_cover_five_states(env, monkeypatch):
    seen = {}

    committed = writer.propose(env.memory, 'remember the alpha fact')
    seen[committed['status']] = committed
    assert committed['memory']['status'] == 'active'
    memory_id = committed['memory']['id']

    merged = writer.propose(env.memory, 'remember the alpha fact')
    seen[merged['status']] = merged
    assert merged['memory']['mention_count'] == 2

    rejected = writer.propose(env.memory, 'blocked fact', settings={'enabled': False})
    seen[rejected['status']] = rejected
    assert rejected['reason'] == 'memory_disabled'

    superseded = writer.correct(env.memory, memory_id, 'remember the beta fact')
    seen[superseded['status']] = superseded

    def fail_write(memory_id, text):
        raise OSError('disk unavailable')

    with monkeypatch.context() as patch:
        patch.setattr(env.memory, 'write_body', fail_write)
        queued = writer.propose(env.memory, 'queued gamma fact')
    seen[queued['status']] = queued
    assert queued['reason'] == 'body_write_failed'
    assert env.memory.get_memory(queued['memory']['id'])['status'] == 'pending'
    assert [item['operation'] for item in env.memory.pending_outbox()] == ['write_body']

    assert set(seen) == set(writer.OUTCOMES)
    assert len(seen) == 5
    assert writer.OUTCOMES == ('committed', 'merged', 'rejected', 'queued', 'superseded')


def test_registered_tick_completes_a_queued_delete(env):
    memory = _propose(env, 'delete me through the tick')['memory']
    event, outcome = env.dispatch('memory.delete', {'memory_id': memory['id']})
    assert event == 'result' and outcome['status'] == writer.QUEUED
    assert env.context.ticks, 'memory commands must register an outbox tick'
    assert env.memory.get_memory(memory['id'])['status'] == 'deleting'

    env.context.ticks[0](0.0)

    assert env.memory.get_memory(memory['id'])['status'] == 'deleted'
    assert not env.memory.body_path(memory['id']).exists()


def test_capability_is_available_and_list_returns_summaries(env):
    event, payload = dispatch({'version': 1, 'id': 'hello', 'type': 'hello',
                               'payload': {'protocol_version': 1}}, env.settings)
    assert event == 'ready'
    assert payload['features']['memory']['available'] is True
    assert payload['features']['memory']['commands'] == [
        'memory.search', 'memory.propose', 'memory.correct', 'memory.delete',
        'memory.list', 'memory.get', 'memory.history', 'memory.archive',
        'memory.restore', 'memory.compact', 'memory.jobs.status',
        'memory.conflicts.list', 'memory.conflicts.resolve',
        'memory.index.rebuild', 'memory.index.status']

    _propose(env, 'first remembered fact')
    _propose(env, 'second remembered fact')
    event, listing = env.dispatch('memory.list', {'limit': 1})
    assert event == 'result'
    assert listing['count'] == 1
    assert listing['memories'][0]['status'] == 'active'

    with pytest.raises(ProtocolError) as caught:
        env.dispatch('memory.search', {'query': ''})
    assert caught.value.code == 'invalid_request'
