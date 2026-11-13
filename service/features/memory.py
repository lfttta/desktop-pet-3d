"""Memory commands backed by the local SQLite store and Markdown bodies.

These commands only accept explicit writes. When ``memory.auto_capture`` is
enabled the chat orchestrator persists model-proposed candidates through the
same writer, so every write keeps the same validation and durability rules.
Every handler resolves the feature store from the request context, so the
transport layer keeps owning the database.
"""

import re
import sqlite3
import time

from service.ai.budget import BudgetLedger
from service.ai.embedding import EmbeddingClient, EmbeddingError
from service.database import SUBJECT_USER_ID
from service.memory import (compact, conflicts, embeddings, jobs,
                            search as memory_search, writer)
from service.memory.store import MemoryStore, memory_summary
from service.protocol import ProtocolError
from service.secrets import SecretsError, load_key_for

NAME = 'memory'
CAPABILITY = {
    'available': True,
    'reason': '',
    'commands': ['memory.search', 'memory.propose', 'memory.correct', 'memory.delete',
                 'memory.list', 'memory.get', 'memory.history', 'memory.archive',
                 'memory.restore', 'memory.compact', 'memory.jobs.status',
                 'memory.conflicts.list', 'memory.conflicts.resolve',
                 'memory.index.rebuild', 'memory.index.status'],
}

DEFAULT_SEARCH_LIMIT = 5
DEFAULT_LIST_LIMIT = 20
MAX_LIMIT = 50
MAX_QUERY_CHARS = 500
MAX_CATEGORY_CHARS = 64
MEMORY_ID_PATTERN = re.compile(r'^[A-Za-z0-9_-]{1,128}$')


def _required_text(payload, key, request_id, maximum=None):
    value = payload.get(key)
    if type(value) is not str or not value.strip():
        raise ProtocolError('invalid_request', f'{key} must be a non-empty string.',
                            request_id)
    if maximum is not None and len(value) > maximum:
        raise ProtocolError('invalid_request', f'{key} exceeds {maximum} characters.',
                            request_id)
    try:
        value.encode('utf-8')
    except UnicodeError:
        raise ProtocolError('invalid_request', f'{key} is not valid Unicode.',
                            request_id) from None
    return value


def _optional_string(payload, key, request_id, maximum):
    if key not in payload:
        return None
    value = payload[key]
    if type(value) is not str or not value or len(value) > maximum:
        raise ProtocolError('invalid_request', f'{key} must be a non-empty string.',
                            request_id)
    return value


def _limit(payload, default, request_id):
    if 'limit' not in payload:
        return default
    value = payload['limit']
    if type(value) is not int or not 1 <= value <= MAX_LIMIT:
        raise ProtocolError('invalid_request', f'limit must be an integer from 1 to {MAX_LIMIT}.',
                            request_id)
    return value


def _memory_id(payload, request_id):
    value = payload.get('memory_id')
    if type(value) is not str or MEMORY_ID_PATTERN.match(value) is None:
        raise ProtocolError('invalid_request', 'memory_id must be a local memory identifier.',
                            request_id)
    return value


def _importance(payload, request_id):
    if 'importance' not in payload:
        return writer.DEFAULT_IMPORTANCE
    value = payload['importance']
    if type(value) not in (int, float) or not 0.0 <= float(value) <= 1.0:
        raise ProtocolError('invalid_request', 'importance must be a number from 0 to 1.',
                            request_id)
    return float(value)


def _source_type(payload, request_id):
    value = payload.get('source_type', 'manual')
    if type(value) is not str or value not in writer.SOURCE_TYPES:
        raise ProtocolError('invalid_request', 'source_type is not supported.', request_id)
    return value


def _memory_settings(store):
    settings = getattr(store, 'settings', None)
    if isinstance(settings, dict) and isinstance(settings.get('memory'), dict):
        return settings['memory']
    return {}


def _memory_store(ctx, request_id):
    database = getattr(ctx, 'database', None)
    if database is None:
        raise ProtocolError('storage_unavailable', 'Memory storage is unavailable.',
                            request_id)
    memory_store = getattr(database, 'memory_store_v3', None)
    if memory_store is None:
        try:
            memory_store = MemoryStore(database)
        except sqlite3.Error:
            raise ProtocolError('storage_unavailable', 'Memory storage is unavailable.',
                                request_id) from None
        setattr(database, 'memory_store_v3', memory_store)
    _register_tick(database, memory_store, ctx)
    return memory_store


def _advanced_memory_store(ctx, request_id):
    """Advanced commands report not_available when no database is wired."""
    if getattr(ctx, 'database', None) is None:
        raise ProtocolError('not_available', 'Memory storage is unavailable.',
                            request_id)
    return _memory_store(ctx, request_id)


TICK_INTERVAL_SECONDS = 5.0


def _register_tick(database, memory_store, ctx):
    if getattr(database, 'memory_tick_v3', False):
        return
    register = getattr(ctx, 'register_tick', None)
    if register is None:
        return
    settings_source = getattr(ctx, 'store', None)
    state = {'at': None}
    # Model compaction performs a provider call. It must not run on the service
    # thread, so the driver owns it while `jobs.tick` skips those jobs. Without
    # a settings source (direct dispatch in tests) there is nothing to drive.
    driver = None
    if settings_source is not None:
        from service.memory.driver import ModelCompactDriver
        driver = ModelCompactDriver(settings_source, memory_store)

    def hook(now):
        writer.process_outbox(memory_store)
        stamp = now if isinstance(now, (int, float)) and not isinstance(now, bool) \
            else time.monotonic()
        if state['at'] is not None and stamp - state['at'] < TICK_INTERVAL_SECONDS:
            return
        state['at'] = stamp
        settings = getattr(settings_source, 'settings', None)
        if not isinstance(settings, dict):
            settings = {}
        deferred = driver is not None
        jobs.tick(memory_store, settings,
                  now=None if isinstance(now, (int, float)) else now,
                  defer_model_compact=deferred)
        if driver is not None:
            driver.tick(now)

    try:
        register(hook)
    except RuntimeError:
        # Contexts without a runtime (direct dispatch in tests) cannot tick.
        return
    if driver is not None:
        # serve() closes it during teardown so the worker never outlives the
        # SQLite connection it reports into.
        setattr(database, 'memory_compact_driver', driver)
    setattr(database, 'memory_tick_v3', True)


def _storage_error(request_id):
    return ProtocolError('storage_unavailable', 'Memory storage operation failed.',
                         request_id)


def _embedding_settings(settings):
    """Return (ai settings, enabled, model, dimensions) from settings."""
    if not isinstance(settings, dict):
        return {}, False, '', 0
    ai_settings = settings.get('ai') if isinstance(settings.get('ai'), dict) else {}
    embedding = ai_settings.get('embedding')
    if not isinstance(embedding, dict):
        return ai_settings, False, '', 0
    model = embedding.get('model')
    model = model if type(model) is str else ''
    dim = embedding.get('dimensions')
    dim = dim if type(dim) is int and not isinstance(dim, bool) else 0
    return ai_settings, bool(embedding.get('enabled')), model, dim


class _StoreEmbedder:
    """Embed through the configured provider and record usage in the ledger.

    The API key is loaded lazily so a keyword-only search never touches the
    secret store. An injected ``embedding_client`` or ``ai_transport`` keeps
    tests free of network access.
    """

    def __init__(self, settings_source, database, ai_settings):
        self.settings_source = settings_source
        self.database = database
        self.ai_settings = ai_settings
        self._client = None

    def _resolve_client(self):
        injected = getattr(self.settings_source, 'embedding_client', None)
        if injected is not None:
            return injected
        if self._client is None:
            self._client = EmbeddingClient(
                self.ai_settings,
                transport=getattr(self.settings_source, 'ai_transport', None))
        return self._client

    def embed(self, texts):
        injected = getattr(self.settings_source, 'embedding_client', None)
        if injected is None:
            injected = getattr(self.settings_source, 'embedding_provider', None)
        if injected is not None:
            # A test provider owns its own credentials.
            method = getattr(injected, 'embed', None)
            outcome = method(texts) if callable(method) else injected(texts)
        else:
            key = getattr(self.settings_source, 'ai_embedding_key', None)
            if key is None:
                key = load_key_for(self.settings_source)
            outcome = self._resolve_client().embed(texts, key=key)
        self._record(outcome)
        return outcome

    def _record(self, outcome):
        usage = getattr(outcome, 'usage', None)
        if not isinstance(usage, dict) or self.database is None:
            return
        model = getattr(outcome, 'model', '')
        if not model:
            model = self.ai_settings.get('embedding', {}).get('model', '')
        try:
            BudgetLedger(self.database).record('embedding', model, usage,
                                               self.ai_settings)
        except Exception:
            # Usage accounting must never break retrieval.
            return


def search(request, store, ctx):
    payload = request['payload']
    query = _required_text(payload, 'query', request['id'], MAX_QUERY_CHARS)
    limit = _limit(payload, DEFAULT_SEARCH_LIMIT, request['id'])
    memory_store = _memory_store(ctx, request['id'])
    settings = getattr(store, 'settings', {}) if store is not None else {}
    if not isinstance(settings, dict):
        settings = {}
    ai_settings, enabled, model, dim = _embedding_settings(settings)
    embedder = None
    if enabled and model and dim > 0:
        embedder = _StoreEmbedder(store, memory_store.database, ai_settings)
    try:
        results = memory_search.search(memory_store, query, limit, settings=settings,
                                       embedder=embedder)
    except sqlite3.Error:
        raise _storage_error(request['id']) from None
    return 'result', {'results': results, 'count': len(results), 'query': query}


def propose(request, store, ctx):
    payload = request['payload']
    text = _required_text(payload, 'text', request['id'])
    category = _optional_string(payload, 'category', request['id'], MAX_CATEGORY_CHARS)
    importance = _importance(payload, request['id'])
    source_type = _source_type(payload, request['id'])
    memory_store = _memory_store(ctx, request['id'])
    try:
        outcome = writer.propose(
            memory_store, text,
            category=category if category is not None else writer.DEFAULT_CATEGORY,
            importance=importance, source_type=source_type,
            settings=_memory_settings(store))
    except sqlite3.Error:
        raise _storage_error(request['id']) from None
    return 'result', outcome


def correct(request, store, ctx):
    payload = request['payload']
    memory_id = _memory_id(payload, request['id'])
    text = _required_text(payload, 'text', request['id'])
    memory_store = _memory_store(ctx, request['id'])
    try:
        outcome = writer.correct(memory_store, memory_id, text,
                                 settings=_memory_settings(store))
    except sqlite3.Error:
        raise _storage_error(request['id']) from None
    return 'result', outcome


def delete(request, store, ctx):
    payload = request['payload']
    memory_id = _memory_id(payload, request['id'])
    memory_store = _memory_store(ctx, request['id'])
    try:
        outcome = writer.delete(memory_store, memory_id)
    except sqlite3.Error:
        raise _storage_error(request['id']) from None
    return 'result', outcome


def list_memories(request, store, ctx):
    payload = request['payload']
    limit = _limit(payload, DEFAULT_LIST_LIMIT, request['id'])
    category = _optional_string(payload, 'category', request['id'], MAX_CATEGORY_CHARS)
    memory_store = _memory_store(ctx, request['id'])
    try:
        rows = memory_store.list_memories(SUBJECT_USER_ID, limit=limit, category=category)
    except sqlite3.Error:
        raise _storage_error(request['id']) from None
    return 'result', {'memories': [memory_summary(row) for row in rows],
                      'count': len(rows)}


def _not_found(request_id):
    return ProtocolError('not_found', 'Memory was not found.', request_id)


def get(request, store, ctx):
    memory_store = _advanced_memory_store(ctx, request['id'])
    memory_id = _memory_id(request['payload'], request['id'])
    try:
        row = memory_store.get_memory(memory_id)
        if row is None or row.get('deleted_at'):
            raise _not_found(request['id'])
        history = memory_store.history_versions(memory_id)
        body = memory_store.read_body(memory_id)
    except sqlite3.Error:
        raise _storage_error(request['id']) from None
    if body is None:
        body = row.get('content', '')
    return 'result', {'memory': memory_summary(row), 'body': body, 'history': history}


def history(request, store, ctx):
    memory_store = _advanced_memory_store(ctx, request['id'])
    memory_id = _memory_id(request['payload'], request['id'])
    try:
        row = memory_store.get_memory(memory_id)
        if row is None or row.get('deleted_at'):
            raise _not_found(request['id'])
        versions = memory_store.history_versions(memory_id)
    except sqlite3.Error:
        raise _storage_error(request['id']) from None
    return 'result', {'versions': versions}


def archive(request, store, ctx):
    memory_store = _advanced_memory_store(ctx, request['id'])
    memory_id = _memory_id(request['payload'], request['id'])
    try:
        outcome = writer.archive(memory_store, memory_id)
    except sqlite3.Error:
        raise _storage_error(request['id']) from None
    if outcome.get('reason') == 'not_found':
        raise _not_found(request['id'])
    if outcome.get('reason') == 'body_move_failed':
        raise ProtocolError('storage_unavailable', 'Memory body could not be moved.',
                            request['id'])
    if outcome.get('status') == writer.REJECTED:
        raise ProtocolError('invalid_state', 'Memory cannot be archived.', request['id'])
    return 'result', {'status': outcome['status'], 'memory_id': memory_id}


def restore(request, store, ctx):
    memory_store = _advanced_memory_store(ctx, request['id'])
    memory_id = _memory_id(request['payload'], request['id'])
    try:
        outcome = writer.restore(memory_store, memory_id)
    except sqlite3.Error:
        raise _storage_error(request['id']) from None
    if outcome.get('reason') == 'not_found':
        raise _not_found(request['id'])
    if outcome.get('reason') == 'body_move_failed':
        raise ProtocolError('storage_unavailable', 'Memory body could not be moved.',
                            request['id'])
    if outcome.get('reason') == 'not_archived':
        raise ProtocolError('invalid_state', 'Memory is not archived.', request['id'])
    return 'result', {'status': outcome['status'], 'memory_id': memory_id}


def compact_memories(request, store, ctx):
    memory_store = _advanced_memory_store(ctx, request['id'])
    payload = request['payload']
    memory_id = None
    if 'memory_id' in payload:
        memory_id = _memory_id(payload, request['id'])
    settings = getattr(store, 'settings', {}) if store is not None else {}
    if not isinstance(settings, dict):
        settings = {}
    try:
        outcome = compact.enqueue(memory_store, settings, memory_id=memory_id)
    except sqlite3.Error:
        raise _storage_error(request['id']) from None
    return 'result', {'queued': outcome['queued'], 'jobs': outcome['jobs']}


def jobs_status(request, store, ctx):
    memory_store = _advanced_memory_store(ctx, request['id'])
    try:
        status = jobs.counts(memory_store)
    except sqlite3.Error:
        raise _storage_error(request['id']) from None
    return 'result', status


def conflicts_list(request, store, ctx):
    memory_store = _advanced_memory_store(ctx, request['id'])
    try:
        conflicts.detect(memory_store)
        rows = conflicts.list_conflicts(memory_store)
    except sqlite3.Error:
        raise _storage_error(request['id']) from None
    return 'result', {'conflicts': rows}


def conflicts_resolve(request, store, ctx):
    memory_store = _advanced_memory_store(ctx, request['id'])
    payload = request['payload']
    conflict_id = payload.get('conflict_id')
    if type(conflict_id) is not str or not conflict_id or len(conflict_id) > 128:
        raise ProtocolError('invalid_request',
                            'conflict_id must be a non-empty string.', request['id'])
    keep = payload.get('keep')
    if keep not in ('a', 'b'):
        raise ProtocolError('invalid_request', "keep must be 'a' or 'b'.", request['id'])
    try:
        outcome = conflicts.resolve(memory_store, conflict_id, keep)
    except sqlite3.Error:
        raise _storage_error(request['id']) from None
    if outcome.get('status') == 'rejected':
        if outcome.get('reason') == 'not_found':
            raise ProtocolError('not_found', 'Conflict was not found.', request['id'])
        if outcome.get('reason') == 'body_move_failed':
            raise ProtocolError('storage_unavailable',
                                'Memory body could not be moved.', request['id'])
        raise ProtocolError('invalid_state', 'Conflict cannot be resolved.', request['id'])
    return 'result', outcome


def index_rebuild(request, store, ctx):
    """Embed every active memory synchronously; returns the indexed count.

    The protocol field is ``queued``; because rebuild runs in the request we
    report the number of memories actually written. Disabled, unconfigured or
    credential-less setups report ``unavailable`` instead of pretending.
    """
    memory_store = _advanced_memory_store(ctx, request['id'])
    settings = getattr(store, 'settings', {}) if store is not None else {}
    if not isinstance(settings, dict):
        settings = {}
    ai_settings, enabled, model, dim = _embedding_settings(settings)
    if not enabled:
        return 'result', {'status': 'unavailable', 'reason': 'disabled'}
    if not model or dim <= 0:
        return 'result', {'status': 'unavailable', 'reason': 'not_configured'}
    embedder = _StoreEmbedder(store, memory_store.database, ai_settings)
    try:
        outcome = embeddings.rebuild(memory_store, model, dim, embedder.embed)
    except SecretsError as exc:
        return 'result', {'status': 'unavailable', 'reason': exc.code}
    except EmbeddingError as exc:
        return 'result', {'status': 'unavailable', 'reason': exc.code}
    except sqlite3.Error:
        raise _storage_error(request['id']) from None
    if outcome['indexed'] == 0 and outcome.get('error'):
        return 'result', {'status': 'unavailable', 'reason': outcome['error']}
    return 'result', {'queued': outcome['indexed']}


def index_status(request, store, ctx):
    memory_store = _advanced_memory_store(ctx, request['id'])
    settings = getattr(store, 'settings', {}) if store is not None else {}
    if not isinstance(settings, dict):
        settings = {}
    _ai_settings, enabled, model, dim = _embedding_settings(settings)
    indexed = embeddings.indexed_count(memory_store, model, dim)
    return 'result', {
        'enabled': enabled,
        'model': model,
        'indexed': indexed,
        'total': embeddings.total_active(memory_store),
        'last_error': embeddings.last_error(memory_store),
    }


COMMANDS = {
    'memory.search': search,
    'memory.propose': propose,
    'memory.correct': correct,
    'memory.delete': delete,
    'memory.list': list_memories,
    'memory.get': get,
    'memory.history': history,
    'memory.archive': archive,
    'memory.restore': restore,
    'memory.compact': compact_memories,
    'memory.jobs.status': jobs_status,
    'memory.conflicts.list': conflicts_list,
    'memory.conflicts.resolve': conflicts_resolve,
    'memory.index.rebuild': index_rebuild,
    'memory.index.status': index_status,
}
