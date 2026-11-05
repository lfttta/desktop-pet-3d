"""Advanced memory: correction chains, archive, compaction and conflicts."""

from datetime import datetime, timedelta, timezone
import pathlib
import sqlite3
import uuid

import pytest

from service.commands import dispatch
from service.database import Database
from service.memory import compact, conflicts, jobs, policy, writer
from service.memory.store import MemoryStore, utc_now
from service.protocol import ProtocolError
from service.settings import SettingsStore


class _Context:
    def __init__(self, database, store=None):
        self.database = database
        self.store = store
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
        self.context = _Context(self.database, self.settings)

    def dispatch(self, command, payload):
        return dispatch({'version': 1, 'id': 'req-memory-advanced', 'type': command,
                         'payload': payload}, self.settings, self.context)

    def close(self):
        self.database.close()


@pytest.fixture
def env(tmp_path):
    environment = _Environment(tmp_path)
    yield environment
    environment.close()


def _propose(env, text):
    event, payload = env.dispatch('memory.propose', {'text': text})
    assert event == 'result'
    return payload['memory']


def _insert(env, content, topic_id=None, importance=0.5, protected=0,
            updated_at=None):
    memory_id = uuid.uuid4().hex
    now = updated_at or utc_now()
    with env.database.transaction() as connection:
        env.memory.insert_memory(connection, {
            'id': memory_id, 'content': content, 'created_at': now,
            'subject_user_id': 'owner', 'category': 'general',
            'importance': importance, 'source_type': 'manual',
            'updated_at': now, 'status': 'active', 'body_hash': '',
            'mention_count': 1, 'session_count': 1, 'recall_count': 0,
            'confidence': 0.5, 'strength': policy.strength(importance),
            'last_reinforced_at': now,
            'dedupe_key': policy.normalize_text(content)[:200],
            'supersedes_id': None, 'fuzz_level': 0, 'memory_level': 'short',
            'topic_id': topic_id, 'protected': protected,
        })
    env.memory.write_body(memory_id, content)
    return memory_id


def _compaction_settings(enabled=True, model_compress=False, min_age_days=0,
                         daily_limit=20, protect_importance=0.8):
    return {'memory': {
        'compaction': {
            'enabled': enabled,
            'model_compress': model_compress,
            'daily_limit': daily_limit,
            'min_age_days': min_age_days,
            'max_per_run': 10,
            'protect_importance': protect_importance,
        },
        'archive': {'enabled': False, 'topic_stale_days': 14},
    }, 'autonomy': {}}


def test_correction_creates_supersedes_chain(env):
    old = _propose(env, '我的猫叫小白')
    event, corrected = env.dispatch('memory.correct',
                                    {'memory_id': old['id'], 'text': '我的猫叫大白'})
    assert event == 'result'
    assert corrected['status'] == writer.SUPERSEDED
    new_id = corrected['memory']['id']

    row = env.memory.get_memory(new_id)
    assert row['supersedes_id'] == old['id']
    assert env.memory.get_memory(old['id'])['status'] == 'superseded'

    event, history = env.dispatch('memory.history', {'memory_id': new_id})
    assert event == 'result'
    versions = history['versions']
    assert [item['id'] for item in versions] == [old['id'], new_id]
    assert set(versions[0]) == {'id', 'text', 'status', 'created_at', 'source_type'}
    assert versions[0]['status'] == 'superseded'
    assert versions[1]['status'] == 'active'

    event, fetched = env.dispatch('memory.get', {'memory_id': new_id})
    assert event == 'result'
    assert fetched['memory']['id'] == new_id
    assert '大白' in fetched['body']
    assert [item['id'] for item in fetched['history']] == [old['id'], new_id]


def test_archive_and_restore_move_body_between_directories(env):
    memory = _propose(env, 'archive round trip memory')
    memory_id = memory['id']
    assert env.memory.body_path(memory_id).is_file()

    event, archived = env.dispatch('memory.archive', {'memory_id': memory_id})
    assert event == 'result'
    assert archived == {'status': 'archived', 'memory_id': memory_id}
    row = env.memory.get_memory(memory_id)
    assert row['status'] == 'archived'
    assert row['fuzz_level'] == policy.FUZZ_ARCHIVED
    assert not env.memory.body_path(memory_id).exists()
    assert env.memory.archive_path(memory_id).is_file()
    assert env.dispatch('memory.search', {'query': 'round trip'})[1]['count'] == 0

    event, fetched = env.dispatch('memory.get', {'memory_id': memory_id})
    assert event == 'result'
    assert 'round trip' in fetched['body']

    event, restored = env.dispatch('memory.restore', {'memory_id': memory_id})
    assert event == 'result'
    assert restored == {'status': 'active', 'memory_id': memory_id}
    row = env.memory.get_memory(memory_id)
    assert row['status'] == 'active'
    assert row['fuzz_level'] == policy.FUZZ_EXACT
    assert env.memory.body_path(memory_id).is_file()
    assert not env.memory.archive_path(memory_id).exists()
    assert env.dispatch('memory.search', {'query': 'round trip'})[1]['count'] == 1

    with pytest.raises(ProtocolError) as caught:
        env.dispatch('memory.restore', {'memory_id': memory_id})
    assert caught.value.code == 'invalid_state'


def test_compaction_commit_creates_version_and_archives_old_body(env):
    text = '今天天气很好。' * 40
    memory_id = _propose(env, text)['id']
    settings = _compaction_settings()

    outcome = compact.process_memory(env.memory, memory_id, settings)
    assert outcome['status'] == writer.SUPERSEDED
    assert outcome['previous_memory_id'] == memory_id
    new_id = outcome['memory']['id']

    old_row = env.memory.get_memory(memory_id)
    new_row = env.memory.get_memory(new_id)
    assert old_row['status'] == 'superseded'
    assert old_row['superseded_by'] == new_id
    assert old_row['fuzz_level'] == policy.FUZZ_EXACT + 1
    assert new_row['status'] == 'active'
    assert new_row['fuzz_level'] == policy.FUZZ_SUMMARY
    assert new_row['supersedes_id'] == memory_id
    assert new_row['source_type'] == 'compaction'
    assert env.memory.archive_path(memory_id).is_file()
    assert not env.memory.body_path(memory_id).exists()
    assert env.memory.body_path(new_id).is_file()
    assert [item['id'] for item in env.memory.version_chain(memory_id)] == \
        [memory_id, new_id]


def test_compaction_failure_keeps_original_and_job_retries(env):
    text = '今天天气很好。' * 40 + '记得买3个苹果。'
    memory_id = _propose(env, text)['id']
    body_before = env.memory.read_body(memory_id)
    hash_before = env.memory.get_memory(memory_id)['body_hash']
    settings = _compaction_settings(model_compress=True)
    assert jobs.enqueue(env.memory, memory_id, jobs.JOB_COMPACT) is not None

    def fail_model(rewrite_text, level):
        raise RuntimeError('model unavailable')

    moment = datetime.now(timezone.utc)
    first = jobs.process(env.memory, settings, moment, compressor=fail_model)
    assert first[0]['status'] == 'retry'
    row = env.memory.get_memory(memory_id)
    assert row['status'] == 'active'
    assert row['fuzz_level'] == policy.FUZZ_EXACT
    assert row['body_hash'] == hash_before
    assert env.memory.read_body(memory_id) == body_before
    assert not env.memory.archive_path(memory_id).exists()

    jobs.process(env.memory, settings, moment + timedelta(minutes=2),
                 compressor=fail_model)
    final = jobs.process(env.memory, settings, moment + timedelta(minutes=4),
                         compressor=fail_model)
    assert final[0]['status'] == 'retry'
    status = jobs.counts(env.memory)
    assert status['failed'] == 1
    assert status['last_error']
    row = env.memory.get_memory(memory_id)
    assert row['status'] == 'active'
    assert env.memory.read_body(memory_id) == body_before
    assert len(env.memory.version_chain(memory_id)) == 1
    job = jobs.list_jobs(env.memory)[0]
    assert job['attempts'] == jobs.MAX_ATTEMPTS
    assert job['status'] == jobs.STATUS_FAILED


def test_compaction_storage_failure_restores_original(env, monkeypatch):
    text = '今天天气很好。' * 40
    memory_id = _propose(env, text)['id']
    body_before = env.memory.read_body(memory_id)
    hash_before = env.memory.get_memory(memory_id)['body_hash']

    def fail_insert(connection, record, or_ignore=False):
        raise sqlite3.OperationalError('metadata write failed')

    with monkeypatch.context() as patch:
        patch.setattr(env.memory, 'insert_memory', fail_insert)
        with pytest.raises(sqlite3.Error):
            writer.commit_compaction(env.memory, memory_id, '今天天气很好。',
                                     policy.FUZZ_SUMMARY)

    row = env.memory.get_memory(memory_id)
    assert row['status'] == 'active'
    assert row['body_hash'] == hash_before
    assert env.memory.read_body(memory_id) == body_before
    assert not env.memory.archive_path(memory_id).exists()
    assert len(list(env.memory.bodies_dir.iterdir())) == 1


def test_archive_body_failure_keeps_memory_active(env, monkeypatch):
    memory_id = _propose(env, 'archive failure memory')['id']

    def fail_move(memory_id):
        raise OSError('archive unavailable')

    with monkeypatch.context() as patch:
        patch.setattr(env.memory, 'archive_body', fail_move)
        with pytest.raises(ProtocolError) as caught:
            env.dispatch('memory.archive', {'memory_id': memory_id})
    assert caught.value.code == 'storage_unavailable'
    assert env.memory.get_memory(memory_id)['status'] == 'active'
    assert env.memory.body_path(memory_id).is_file()


def test_key_field_validation_rejects_lossy_rewrites(env):
    original = '2024年5月1日我不吃3个苹果'
    missing = policy.missing_key_fields(original, '2024年5月1日吃3个苹果')
    assert missing == ['negation:不']
    assert not policy.preserves_key_fields(original, '不吃苹果')
    assert 'number:3' in policy.missing_key_fields(original, '不吃苹果')
    assert policy.preserves_key_fields(original, '2024年5月1日 不吃3个苹果')

    long_text = '开头。' * 100 + '2024年5月1日的3个苹果不要丢。' + '结尾。' * 100
    assert policy.summarize(long_text, limit=200) is None
    assert policy.apply_fuzz(long_text, policy.FUZZ_SUMMARY) is None

    with pytest.raises(compact.CompactionError) as caught:
        compact.parse_compressed('not json')
    assert caught.value.code == 'invalid_json'

    memory_id = _propose(env, original)['id']
    settings = _compaction_settings(model_compress=True)
    with pytest.raises(compact.CompactionError) as caught:
        compact.process_memory(
            env.memory, memory_id, settings,
            compressor=lambda text, level: '{"text": "苹果"}')
    assert caught.value.code == 'key_fields_lost'
    assert env.memory.get_memory(memory_id)['status'] == 'active'
    assert env.memory.read_body(memory_id) == '# Memory\n\n' + original + '\n'


def test_conflict_detection_and_resolution(env):
    first = _insert(env, '猫的生日是3月5日', topic_id='cat-birthday')
    second = _insert(env, '猫的生日是3月6日', topic_id='cat-birthday')

    event, listing = env.dispatch('memory.conflicts.list', {})
    assert event == 'result'
    assert len(listing['conflicts']) == 1
    conflict = listing['conflicts'][0]
    assert conflict['topic_key'] == 'cat-birthday'
    assert conflict['status'] == 'open'
    assert {conflict['memory_a'], conflict['memory_b']} == {first, second}
    assert env.memory.get_memory(first)['status'] == 'disputed'
    assert env.memory.get_memory(second)['status'] == 'disputed'

    event, resolved = env.dispatch('memory.conflicts.resolve', {
        'conflict_id': conflict['id'], 'keep': 'a'})
    assert event == 'result'
    assert resolved == {'status': 'resolved',
                        'kept': conflict['memory_a'],
                        'archived': conflict['memory_b']}
    assert env.memory.get_memory(conflict['memory_a'])['status'] == 'active'
    assert env.memory.get_memory(conflict['memory_b'])['status'] == 'archived'
    assert env.memory.archive_path(conflict['memory_b']).is_file()
    assert not env.memory.body_path(conflict['memory_b']).exists()

    event, listing = env.dispatch('memory.conflicts.list', {})
    assert listing['conflicts'][0]['status'] == 'resolved'

    with pytest.raises(ProtocolError) as caught:
        env.dispatch('memory.conflicts.resolve', {
            'conflict_id': 'missing-conflict', 'keep': 'a'})
    assert caught.value.code == 'not_found'


def test_jobs_gating_pause_and_status(env):
    memory_id = _propose(env, 'job queue gating memory')['id']
    settings = _compaction_settings()

    jobs.pause(env.memory)
    assert jobs.enqueue(env.memory, memory_id, jobs.JOB_COMPACT) is not None
    assert jobs.tick(env.memory, settings) == []
    assert jobs.counts(env.memory)['pending'] == 1
    jobs.resume(env.memory)

    assert jobs.should_run(env.memory, settings, pending_count=jobs.QUEUE_THRESHOLD)
    with env.database.transaction() as connection:
        connection.execute(
            "INSERT INTO conversations(id,title,subject_user_id,created_at) "
            "VALUES ('c1','chat','owner',?)", (utc_now(),))
        connection.execute(
            "INSERT INTO messages(id,conversation_id,role,content,subject_user_id,"
            "created_at) VALUES ('m1','c1','user','hello','owner',?)",
            (utc_now(),))
    assert not jobs.should_run(env.memory, settings, pending_count=0)

    event, status = env.dispatch('memory.jobs.status', {})
    assert event == 'result'
    assert set(status) == {'pending', 'running', 'failed', 'completed', 'last_error'}
    assert status['pending'] == 1


def test_compact_command_skips_protected_and_important(env):
    env.settings.update({'memory': {'compaction': {'enabled': True,
                                                   'min_age_days': 0}}})
    protected = _insert(env, 'protected memory', protected=1)
    important = _insert(env, 'very important memory', importance=0.9)
    normal = _insert(env, 'ordinary memory', importance=0.4)

    event, payload = env.dispatch('memory.compact', {})
    assert event == 'result'
    assert payload['queued'] == 1
    assert payload['jobs'][0]['memory_id'] == normal

    event, payload = env.dispatch('memory.compact', {'memory_id': protected})
    assert event == 'result'
    assert payload['queued'] == 0
    event, payload = env.dispatch('memory.compact', {'memory_id': important})
    assert event == 'result'
    assert payload['queued'] == 0

    settings = _compaction_settings(daily_limit=0)
    assert compact.enqueue(env.memory, settings)['queued'] == 0
