"""Single dispatch registry for service commands.

Every command is a handler(request, store, ctx) returning (event, payload) or
None when the handler already emitted its own frames through ctx. Adding a
feature means registering its commands here and publishing a capability; the
transport loop and error envelopes stay unchanged.
"""

from service import PROTOCOL_VERSION, SERVICE_VERSION
from service.features import (ai, autonomy, capabilities, chat, desktop, diag, expression,
                              growth, memory, state, usage, voice)
from service.persona import public_catalog
from service.protocol import ProtocolError
from service.settings import ValidationError


RESERVED_COMMANDS = frozenset()
RESERVED_PREFIXES = (
    'voice', 'tts', 'action', 'personality',
    'memory', 'memory.fuzzy', 'memory.index', 'user',
)


class _NullLog:
    def record(self, level, module, code, message=''):
        pass

    def tail(self, limit=0):
        return []


class _NullContext:
    store = None
    database = None
    log = _NullLog()

    def emit(self, event, payload):
        raise RuntimeError('event emission requires a transport context')

    def post(self, event, payload):
        raise RuntimeError('event emission requires a transport context')

    def register_tick(self, hook):
        raise RuntimeError('tick registration requires a runtime context')


def _hello(request, store, ctx):
    payload = request['payload']
    if type(payload.get('protocol_version')) is not int:
        raise ProtocolError('invalid_request', 'hello requires integer protocol_version.', request['id'])
    if payload['protocol_version'] != PROTOCOL_VERSION:
        raise ProtocolError('unsupported_version', 'Only protocol version 1 is supported.', request['id'])
    return 'ready', {
        'protocol_version': PROTOCOL_VERSION,
        'service_version': SERVICE_VERSION,
        'settings': store.settings,
        'warnings': store.warnings,
        'features': capabilities(),
        'personas': public_catalog(),
    }


def _settings_get(request, store, ctx):
    return 'result', {'settings': store.settings}


def _settings_update(request, store, ctx):
    try:
        saved = store.update(request['payload'])
    except ValidationError as exc:
        raise ProtocolError('invalid_settings', str(exc), request['id']) from None
    except OSError:
        raise ProtocolError('save_failed', 'Settings were not saved; last successful values retained.',
                            request['id']) from None
    return 'result', {'settings': saved}


def _shutdown(request, store, ctx):
    return 'result', {}


def _not_available(request, store, ctx):
    raise ProtocolError('not_available', 'This feature is reserved for a later stage.', request['id'])


HANDLERS = {
    'hello': _hello,
    'settings.get': _settings_get,
    'settings.update': _settings_update,
    'shutdown': _shutdown,
    **chat.COMMANDS,
    **memory.COMMANDS,
    **state.COMMANDS,
    **autonomy.COMMANDS,
    **ai.COMMANDS,
    **expression.COMMANDS,
    **growth.COMMANDS,
    **usage.COMMANDS,
    **diag.COMMANDS,
    **desktop.COMMANDS,
    **voice.COMMANDS,
}


def _is_reserved(command):
    if command in RESERVED_COMMANDS:
        return True
    return any(command == prefix or command.startswith(prefix + '.')
               for prefix in RESERVED_PREFIXES)


def dispatch(request, store, ctx=None):
    context = _NullContext() if ctx is None else ctx
    handler = HANDLERS.get(request['type'])
    if handler is not None:
        return handler(request, store, context)
    if _is_reserved(request['type']):
        return _not_available(request, store, context)
    raise ProtocolError('unknown_command', 'Unknown command.', request['id'])
