"""Context retrieval: combined ranking, recall growth, budget and fallback."""

import pathlib

import pytest

from service.ai.provider import TransportError
from service.database import Database
from service.memory import embeddings as memory_embeddings
from service.memory import policy
from service.memory.search import search_for_context
from service.memory.store import MemoryStore
from service.settings import SettingsStore

EMBEDDING_MODEL = 'fake-embed'
EMBEDDING_DIM = 3
FAKE_KEY = 'fake-key-not-real'
NOW = '2026-11-13T00:00:00.000000Z'
OLD = '2026-01-01T00:00:00.000000Z'


def _insert(store, memory_id, content, **overrides):
    fields = {
        'id': memory_id,
        'content': content,
        'created_at': NOW,
        'subject_user_id': 'owner',
        'category': 'general',
        'importance': 0.5,
        'source_type': 'manual',
        'updated_at': NOW,
        'status': 'active',
        'body_hash': '',
        'mention_count': 1,
        'session_count': 1,
        'recall_count': 0,
        'confidence': 0.5,
        'last_reinforced_at': NOW,
        'dedupe_key': content,
    }
    fields.update(overrides)
    fields['strength'] = policy.strength(
        fields['importance'], fields['mention_count'], fields['session_count'],
        fields['recall_count'], fields['confidence'])
    with store.database.transaction() as connection:
        store.insert_memory(connection, fields)
    store.index_memory(memory_id, content)


@pytest.fixture
def env(tmp_path):
    database = Database(pathlib.Path(tmp_path) / 'pet.db')
    store = MemoryStore(database)
    database.memory_store_v3 = store
    try:
        yield database, store
    finally:
        database.close()


class _FailingTransport:
    def __init__(self):
        self.calls = 0

    def send(self, url, headers, body, connect_timeout, read_timeout):
        self.calls += 1
        raise TransportError('timeout', 'connection timed out')


def test_ranking_combines_signals_not_only_hit_count(env):
    database, store = env
    _insert(store, 'two-hits', 'cat dog', updated_at=OLD)
    _insert(store, 'one-hit', 'cat', importance=1.0, mention_count=20,
            session_count=10, recall_count=50, confidence=1.0)

    results = search_for_context(database, 'cat dog', {}, 5, 500)

    assert results[0] == 'cat'
    assert results[1] == 'cat dog'


def test_returned_memories_increment_recall_and_strength(env):
    database, store = env
    _insert(store, 'recalled', '我喜欢下雨天')
    _insert(store, 'untouched', '别的记忆')
    baseline = store.get_memory('recalled')['strength']

    assert search_for_context(database, '下雨天', {}, 5, 500) == ['我喜欢下雨天']
    first = store.get_memory('recalled')
    assert first['recall_count'] == 1
    assert first['strength'] > baseline

    assert search_for_context(database, '下雨天', {}, 5, 500) == ['我喜欢下雨天']
    second = store.get_memory('recalled')
    assert second['recall_count'] == 2
    assert second['strength'] > first['strength']
    assert store.get_memory('untouched')['recall_count'] == 0


def test_budget_is_strict_and_prefers_higher_scores(env):
    database, store = env
    _insert(store, 'top', '猫' * 20, importance=1.0, mention_count=10,
            recall_count=10, confidence=1.0)
    _insert(store, 'second', '猫' * 5, updated_at=OLD)

    results = search_for_context(database, '猫', {}, 5, 10)

    assert results == ['猫' * 10]
    assert sum(len(text) for text in results) <= 10
    # The entry dropped by the budget must not strengthen its own ranking.
    assert store.get_memory('top')['recall_count'] == 11
    assert store.get_memory('second')['recall_count'] == 0


def test_dict_settings_with_embedding_enabled_does_not_record_missing_key(env):
    database, store = env
    _insert(store, 'cat', 'my cat is called snow')
    settings = {'ai': {'embedding': {
        'enabled': True, 'model': EMBEDDING_MODEL, 'dimensions': EMBEDDING_DIM}}}

    # A bare settings dict has no key provider. Keyword retrieval still works
    # and the diagnostic error must not be overwritten with missing_key.
    assert search_for_context(database, 'cat', settings, 5, 500) == [
        'my cat is called snow']
    assert memory_embeddings.last_error(store) != 'missing_key'


def test_embedding_failure_falls_back_to_keyword(env, tmp_path):
    database, store = env
    _insert(store, 'cat', 'my cat is called snow')
    settings = SettingsStore(tmp_path)
    settings.update({'ai': {'embedding': {
        'enabled': True, 'model': EMBEDDING_MODEL, 'dimensions': EMBEDDING_DIM}}})
    transport = _FailingTransport()
    settings.ai_transport = transport
    settings.ai_key_provider = lambda: FAKE_KEY

    # One keyword hit is below the vector threshold, so one embedding request
    # is attempted; its failure must still return the keyword result.
    assert search_for_context(database, 'cat', settings, 5, 500) == [
        'my cat is called snow']
    assert transport.calls == 1
    assert store.get_memory('cat')['recall_count'] == 1
