"""Vector retrieval: rebuild, ranking, model mismatch, fallback and usage.

The embedding provider is always a fake transport, so these tests never touch
the network or the DPAPI secret store.
"""

import json
import pathlib

import pytest

from service.ai.embedding import EmbeddingClient, EmbeddingError, EmbeddingResult
from service.ai.provider import TransportError
from service.commands import dispatch
from service.database import Database
from service.memory.store import MemoryStore
from service.settings import SettingsStore

EMBEDDING_MODEL = 'fake-embed'
EMBEDDING_DIM = 3
FAKE_KEY = 'fake-key-not-real'


def _vector_for(text):
    lowered = text.casefold()
    if 'cat' in lowered or '猫' in lowered or 'feline' in lowered:
        return [1.0, 0.0, 0.0]
    if 'dog' in lowered or '狗' in lowered:
        return [0.0, 1.0, 0.0]
    return [0.0, 0.0, 1.0]


class FakeResponse:
    def __init__(self, body, headers=None):
        self._body = body
        self.headers = headers or {'Content-Type': 'application/json'}
        self.closed = False

    def read(self, size=-1):
        if size is None or size < 0:
            chunk, self._body = self._body, b''
            return chunk
        chunk, self._body = self._body[:size], self._body[size:]
        return chunk

    def close(self):
        self.closed = True


class FakeEmbeddingTransport:
    """Deterministic OpenAI-compatible embeddings endpoint."""

    def __init__(self):
        self.requests = []
        self.calls = 0
        self.fail_with = None

    def send(self, url, headers, body, connect_timeout, read_timeout):
        self.calls += 1
        payload = json.loads(body.decode('utf-8'))
        self.requests.append({'url': url, 'headers': dict(headers), 'body': payload})
        if self.fail_with is not None:
            raise self.fail_with
        vectors = [_vector_for(text) for text in payload['input']]
        tokens = sum(len(text) for text in payload['input'])
        reply = {
            'object': 'list',
            'model': payload.get('model') or EMBEDDING_MODEL,
            'data': [{'object': 'embedding', 'index': index, 'embedding': vector}
                     for index, vector in enumerate(vectors)],
            'usage': {'prompt_tokens': tokens, 'total_tokens': tokens},
        }
        return FakeResponse(json.dumps(reply).encode('utf-8'))


class StaticTransport:
    def __init__(self, body):
        self.body = body
        self.calls = 0

    def send(self, url, headers, body, connect_timeout, read_timeout):
        self.calls += 1
        if isinstance(self.body, Exception):
            raise self.body
        return FakeResponse(self.body)


class FakeEmbeddingProvider:
    """Injected provider used to prove no credentials are needed in tests."""

    def __init__(self):
        self.calls = 0

    def embed(self, texts):
        self.calls += 1
        return EmbeddingResult(
            vectors=[_vector_for(text) for text in texts],
            model=EMBEDDING_MODEL,
            usage={'prompt_tokens': 1, 'completion_tokens': 0, 'total_tokens': 1},
            dim=EMBEDDING_DIM,
        )


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
        self.transport = FakeEmbeddingTransport()
        self.settings.ai_transport = self.transport
        self.settings.ai_key_provider = lambda: FAKE_KEY

    def dispatch(self, command, payload):
        return dispatch({'version': 1, 'id': 'req-vector', 'type': command,
                         'payload': payload}, self.settings, self.context)

    def close(self):
        self.database.close()


@pytest.fixture
def env(tmp_path):
    environment = _Environment(tmp_path)
    yield environment
    environment.close()


def _enable(env, model=EMBEDDING_MODEL, dimensions=EMBEDDING_DIM, enabled=True):
    env.settings.update({'ai': {'embedding': {
        'enabled': enabled, 'model': model, 'dimensions': dimensions}}})
    env.settings.ai_transport = env.transport
    env.settings.ai_key_provider = lambda: FAKE_KEY


def _propose(env, text):
    event, payload = env.dispatch('memory.propose', {'text': text})
    assert event == 'result'
    return payload['memory']


def _search(env, query, limit=None):
    body = {'query': query}
    if limit is not None:
        body['limit'] = limit
    event, payload = env.dispatch('memory.search', body)
    assert event == 'result'
    return payload


def test_rebuild_and_status_report_indexed_rows(env):
    _enable(env)
    cat = _propose(env, 'my cat is called snow')
    _propose(env, 'my dog is called mud')

    event, status = env.dispatch('memory.index.status', {})
    assert event == 'result'
    assert status == {'enabled': True, 'model': EMBEDDING_MODEL, 'indexed': 0,
                      'total': 2, 'last_error': ''}

    event, rebuilt = env.dispatch('memory.index.rebuild', {})
    assert event == 'result'
    assert rebuilt == {'queued': 2}
    assert env.transport.calls == 1
    assert env.transport.requests[0]['url'] == 'https://api.deepseek.com/v1/embeddings'
    assert env.transport.requests[0]['headers']['Authorization'] == 'Bearer ' + FAKE_KEY
    assert env.transport.requests[0]['body']['model'] == EMBEDDING_MODEL

    event, status = env.dispatch('memory.index.status', {})
    assert status['indexed'] == 2
    assert status['total'] == 2
    assert env.memory.get_memory(cat['id'])['status'] == 'active'
    assert env.database.connection.execute(
        'SELECT COUNT(*) FROM memory_embeddings').fetchone()[0] == 2


def test_vector_fallback_ranks_when_keyword_misses(env):
    _enable(env)
    cat = _propose(env, 'my cat is called snow')
    _propose(env, 'my dog is called mud')
    env.dispatch('memory.index.rebuild', {})
    after_rebuild = env.transport.calls

    payload = _search(env, 'feline')
    assert payload['count'] == 1
    assert payload['results'][0]['id'] == cat['id']
    assert payload['results'][0]['score'] == pytest.approx(1.0)
    # Exactly one embedding request for the query.
    assert env.transport.calls == after_rebuild + 1

    # A keyword hit keeps the existing path and never calls the provider.
    payload = _search(env, 'cat')
    assert payload['results'][0]['id'] == cat['id']
    assert env.transport.calls == after_rebuild + 1


def test_model_mismatch_skips_stored_vectors(env):
    _enable(env, model='model-a')
    _propose(env, 'my cat is called snow')
    env.dispatch('memory.index.rebuild', {})
    assert env.transport.calls == 1

    _enable(env, model='model-b')
    payload = _search(env, 'feline')
    assert payload['count'] == 0
    assert env.transport.requests[-1]['body']['model'] == 'model-b'

    event, status = env.dispatch('memory.index.status', {})
    assert status['model'] == 'model-b'
    assert status['indexed'] == 0
    assert status['total'] == 1


def test_embedding_failure_falls_back_and_records_error(env):
    _enable(env)
    cat = _propose(env, 'my cat is called snow')
    env.dispatch('memory.index.rebuild', {})
    env.transport.fail_with = TransportError('timeout', 'connection timed out')

    payload = _search(env, 'feline')
    assert payload['count'] == 0
    event, status = env.dispatch('memory.index.status', {})
    assert status['last_error'] == 'timeout'

    # Keyword retrieval still works while the provider is down.
    payload = _search(env, 'cat')
    assert payload['results'][0]['id'] == cat['id']
    assert payload['count'] == 1
    assert env.transport.calls == 2  # rebuild plus the one failed query


def test_missing_table_and_damaged_vector_do_not_break_search(env):
    _enable(env)
    cat = _propose(env, 'my cat is called snow')
    env.dispatch('memory.index.rebuild', {})

    with env.database.transaction() as connection:
        connection.execute("UPDATE memory_embeddings SET vector=?",
                           (b'damaged',))
    assert _search(env, 'feline')['count'] == 0
    assert _search(env, 'cat')['results'][0]['id'] == cat['id']

    with env.database.transaction() as connection:
        connection.execute('DROP TABLE memory_embeddings')
    assert _search(env, 'feline')['count'] == 0
    assert _search(env, 'cat')['results'][0]['id'] == cat['id']
    event, status = env.dispatch('memory.index.status', {})
    assert status['indexed'] == 0
    assert status['total'] == 1


def test_exhausted_budget_skips_embedding_and_usage_still_reports(env):
    _enable(env)
    _propose(env, 'my cat is called snow')
    env.dispatch('memory.index.rebuild', {})
    assert env.transport.calls == 1

    env.settings.update({'ai': {'daily_token_budget': 1}})
    payload = _search(env, 'feline')
    assert payload['count'] == 0
    assert env.transport.calls == 1

    event, usage = env.dispatch('usage.get', {})
    assert event == 'result'
    assert usage['status'] == 'hard_limit'
    assert usage['today']['calls'] >= 1
    assert usage['today']['tokens'] > 0
    assert usage['by_model'][0]['model'] == EMBEDDING_MODEL


def test_rebuild_reports_unavailable_when_disabled_or_unconfigured(env):
    event, payload = env.dispatch('memory.index.rebuild', {})
    assert event == 'result'
    assert payload == {'status': 'unavailable', 'reason': 'disabled'}
    assert env.transport.calls == 0

    _enable(env, model='', dimensions=0)
    event, payload = env.dispatch('memory.index.rebuild', {})
    assert payload == {'status': 'unavailable', 'reason': 'not_configured'}
    assert env.transport.calls == 0


def test_injected_provider_works_without_credentials(env):
    _enable(env)
    env.settings.ai_key_provider = None
    provider = FakeEmbeddingProvider()
    env.settings.embedding_client = provider
    cat = _propose(env, 'my cat is called snow')

    event, rebuilt = env.dispatch('memory.index.rebuild', {})
    assert rebuilt == {'queued': 1}
    assert provider.calls == 1

    payload = _search(env, 'feline')
    assert payload['results'][0]['id'] == cat['id']
    assert provider.calls == 2


def test_embedding_client_rejects_malformed_replies():
    wrong_count = json.dumps({
        'data': [{'index': 0, 'embedding': [1.0, 0.0, 0.0]}],
    }).encode('utf-8')
    client = EmbeddingClient({'embedding': {'model': 'm', 'dimensions': 3}},
                             StaticTransport(wrong_count))
    with pytest.raises(EmbeddingError) as caught:
        client.embed(['one', 'two'], key='fake')
    assert caught.value.code == 'invalid_response'

    wrong_dim = json.dumps({
        'data': [{'index': 0, 'embedding': [1.0, 0.0]}],
    }).encode('utf-8')
    client = EmbeddingClient({'embedding': {'model': 'm', 'dimensions': 3}},
                             StaticTransport(wrong_dim))
    with pytest.raises(EmbeddingError) as caught:
        client.embed(['one'], key='fake')
    assert caught.value.code == 'dimension_mismatch'

    client = EmbeddingClient({'embedding': {'model': 'm', 'dimensions': 3}},
                             StaticTransport(b''))
    with pytest.raises(EmbeddingError) as caught:
        client.embed(['one'], key='')
    assert caught.value.code == 'missing_key'
