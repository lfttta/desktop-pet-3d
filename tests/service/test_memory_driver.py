"""Background model-compaction driver: worker threading, retries and shutdown.

The service thread must never perform the provider call for model compaction.
These tests drive the real ``TaskRunner`` with a scripted transport and assert
that the worker performs the call while the service thread keeps all SQLite
writes and the job bookkeeping.
"""

import json
import pathlib
import threading
import time
import uuid

import pytest

from service.database import Database
from service.memory import driver, jobs, policy, writer
from service.memory.store import MemoryStore, utc_now
from service.settings import SettingsStore

FAKE_KEY = 'driver-test-key-not-real'


class FakeResponse:
    def __init__(self, body=b'', headers=None):
        self._body = body
        self.headers = headers or {'Content-Type': 'application/json'}

    def read(self, size=-1):
        if size is None or size < 0:
            chunk, self._body = self._body, b''
            return chunk
        chunk, self._body = self._body[:size], self._body[size:]
        return chunk

    def close(self):
        pass


class ScriptedTransport:
    """Records requests and returns a scripted reply (or raises it)."""

    def __init__(self, scripts):
        self.scripts = list(scripts)
        self.requests = []
        self.threads = []
        self.calls = 0

    def send(self, url, headers, body, connect_timeout, read_timeout):
        self.threads.append(threading.get_ident())
        self.requests.append(json.loads(body.decode('utf-8')))
        index = min(self.calls, len(self.scripts) - 1)
        item = self.scripts[index]
        self.calls += 1
        if isinstance(item, Exception):
            raise item
        return item


def json_reply(content):
    body = json.dumps({
        'model': 'deepseek-chat',
        'choices': [{'message': {'role': 'assistant', 'content': content},
                     'finish_reason': 'stop'}],
        'usage': {'prompt_tokens': 50, 'completion_tokens': 10,
                  'total_tokens': 60},
    }, ensure_ascii=False).encode('utf-8')
    return FakeResponse(body=body)


class Clock:
    def __init__(self, start=1000.0):
        self.value = start

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class Env:
    def __init__(self, tmp_path, transport):
        self.directory = pathlib.Path(tmp_path)
        self.database = Database(self.directory / 'pet.db')
        self.settings = SettingsStore(tmp_path)
        self.settings.ai_key_provider = lambda: FAKE_KEY
        self.settings.ai_transport = transport
        self.memory = MemoryStore(self.database)
        self.clock = Clock()

    def enable_model_compaction(self):
        self.settings.update({'memory': {'compaction': {
            'enabled': True, 'model_compress': True,
            'min_age_days': 0, 'daily_limit': 20,
        }}})

    def insert_memory(self, content, importance=0.5, protected=0):
        memory_id = uuid.uuid4().hex
        now = utc_now()
        with self.database.transaction() as connection:
            self.memory.insert_memory(connection, {
                'id': memory_id, 'content': content, 'created_at': now,
                'subject_user_id': 'owner', 'category': 'general',
                'importance': importance, 'source_type': 'manual',
                'updated_at': now, 'status': 'active', 'body_hash': '',
                'mention_count': 1, 'session_count': 1, 'recall_count': 0,
                'confidence': 0.5, 'strength': policy.strength(importance),
                'last_reinforced_at': now,
                'dedupe_key': policy.normalize_text(content)[:200],
                'supersedes_id': None, 'fuzz_level': 0, 'memory_level': 'short',
                'topic_id': None, 'protected': protected,
            })
        self.memory.write_body(memory_id, content)
        return memory_id

    def queued_compact(self, content='今天天气很好。' * 40):
        memory_id = self.insert_memory(content)
        job = jobs.enqueue(self.memory, memory_id, jobs.JOB_COMPACT)
        assert job is not None
        return memory_id, job['id']

    def make_driver(self):
        return driver.ModelCompactDriver(self.settings, self.memory,
                                         clock=self.clock)

    def job_row(self, job_id):
        row = self.memory.connection.execute(
            'SELECT status, attempts FROM memory_jobs WHERE id=?',
            (job_id,)).fetchone()
        return {'status': row[0], 'attempts': row[1]} if row else None

    def close(self):
        self.database.close()


@pytest.fixture
def transport():
    return ScriptedTransport([json_reply('{"text": "天气不错。"}')])


@pytest.fixture
def env(tmp_path, transport):
    environment = Env(tmp_path, transport)
    yield environment
    environment.close()


def _drive_until(env, instance, predicate, timeout=5.0):
    """Tick until ``predicate()`` holds; the driver polls on every tick."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        instance.tick()
        if predicate():
            return True
        time.sleep(0.005)
    return False


def test_driver_runs_the_provider_call_off_the_service_thread(env, transport):
    env.enable_model_compaction()
    memory_id, job_id = env.queued_compact()
    instance = env.make_driver()
    try:
        service_thread = threading.get_ident()
        assert _drive_until(env, instance, lambda: env.job_row(job_id)['status'] == 'completed')
        # The request really happened, and on a different thread.
        assert len(transport.requests) == 1
        assert transport.threads[0] != service_thread
        # The compaction committed through the normal writer path.
        old = env.memory.get_memory(memory_id)
        assert old['status'] == writer.SUPERSEDED
        assert old['superseded_by'] is not None
        assert env.memory.get_memory(old['superseded_by'])['status'] == 'active'
    finally:
        instance.close()


def test_driver_retries_when_the_provider_fails(env):
    env.settings.ai_transport = ScriptedTransport([RuntimeError('provider down')])
    env.enable_model_compaction()
    memory_id, job_id = env.queued_compact()
    instance = env.make_driver()
    try:
        assert _drive_until(env, instance,
                            lambda: env.job_row(job_id)['status'] == 'pending')
        row = env.job_row(job_id)
        assert row['attempts'] == 1
        # The original memory is untouched: nothing was committed.
        assert env.memory.get_memory(memory_id)['status'] == 'active'
        assert env.memory.get_memory(memory_id)['fuzz_level'] == policy.FUZZ_EXACT
    finally:
        instance.close()


def test_driver_retries_when_no_key_is_configured(env):
    env.settings.ai_key_provider = None
    env.enable_model_compaction()
    _memory_id, job_id = env.queued_compact()
    instance = env.make_driver()
    try:
        assert _drive_until(env, instance,
                            lambda: env.job_row(job_id)['status'] == 'pending')
        # No transport call: the compressor was never built.
        assert env.job_row(job_id)['attempts'] == 1
    finally:
        instance.close()


def test_driver_checks_are_throttled(env, monkeypatch):
    env.enable_model_compaction()
    instance = env.make_driver()
    checks = []
    original = jobs.next_model_job

    def counting(store, settings, moment):
        checks.append(moment)
        return original(store, settings, moment)

    monkeypatch.setattr(jobs, 'next_model_job', counting)
    try:
        for _ in range(5):
            instance.tick()
        # Still inside CHECK_SECONDS: exactly one look at the queue.
        assert len(checks) == 1
        env.clock.advance(driver.CHECK_SECONDS)
        instance.tick()
        assert len(checks) == 2
    finally:
        instance.close()


def test_driver_stops_after_close(env, transport):
    env.enable_model_compaction()
    _memory_id, job_id = env.queued_compact()
    instance = env.make_driver()
    instance.close()
    instance.tick()
    assert env.job_row(job_id)['status'] == 'pending'
    assert transport.requests == []


def test_driver_skips_protected_memories_without_calling_the_provider(env, transport):
    env.enable_model_compaction()
    memory_id = env.insert_memory('今天天气很好。' * 40, protected=1)
    job = jobs.enqueue(env.memory, memory_id, jobs.JOB_COMPACT)
    assert job is not None
    instance = env.make_driver()
    try:
        assert _drive_until(env, instance,
                            lambda: env.job_row(job['id'])['status'] == 'completed')
        # next_model_job marked it running, prepare skipped it, no request made.
        assert transport.requests == []
        assert env.memory.get_memory(memory_id)['status'] == 'active'
    finally:
        instance.close()
