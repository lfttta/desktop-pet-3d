"""AI credential and provider-test commands.

Keys are only ever written through the DPAPI secret store; they are never
echoed in a response, persisted in settings, or logged.
"""

import time

from service.ai.provider import OpenAICompatibleProvider, ProviderError
from service.protocol import ProtocolError
from service.secrets import SecretsError, load_key_for, secret_store_for

MAX_KEY_LENGTH = 4096


def _key(request, store):
    try:
        return load_key_for(store)
    except SecretsError as exc:
        raise ProtocolError(exc.code, exc.message, request['id']) from None


def set_key(request, store, ctx):
    key = request['payload'].get('key')
    if type(key) is not str or not key.strip() or len(key) > MAX_KEY_LENGTH:
        raise ProtocolError('invalid_request', 'ai.set_key requires a non-empty key string.', request['id'])
    try:
        secret_store_for(store).save(key)
    except SecretsError as exc:
        raise ProtocolError(exc.code, exc.message, request['id']) from None
    return 'result', {'saved': True}


def clear_key(request, store, ctx):
    try:
        cleared = secret_store_for(store).clear()
    except SecretsError as exc:
        raise ProtocolError(exc.code, exc.message, request['id']) from None
    return 'result', {'cleared': bool(cleared)}


def test(request, store, ctx):
    key = _key(request, store)
    settings = store.settings.get('ai')
    if not isinstance(settings, dict):
        settings = {}
    transport = getattr(store, 'ai_transport', None)
    provider = OpenAICompatibleProvider(settings, transport=transport)
    started = time.monotonic()
    try:
        completion = provider.probe(key)
    except ProviderError as exc:
        raise ProtocolError(exc.code, 'Provider test failed (%s).' % exc.code,
                            request['id']) from None
    except Exception:
        raise ProtocolError('internal_error', 'Provider test failed.',
                            request['id']) from None
    return 'result', {
        'ok': True,
        'model': completion.model or settings.get('model') or '',
        'provider': settings.get('provider') or 'deepseek',
        'latency_ms': int((time.monotonic() - started) * 1000),
    }


COMMANDS = {'ai.set_key': set_key, 'ai.clear_key': clear_key, 'ai.test': test}
