"""Single dispatch registry for service commands.

Every command is a handler(request, store) returning (event, payload). Adding a
feature means registering its commands here and publishing a capability; the
transport loop and error envelopes stay unchanged.
"""

from service import PROTOCOL_VERSION, SERVICE_VERSION
from service.features import capabilities, chat
from service.persona import public_catalog
from service.protocol import ProtocolError
from service.settings import ValidationError

RESERVED_COMMANDS = frozenset({'tts.synthesize'})
RESERVED_PREFIX = 'memory'


def _hello(request, store):
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


def _settings_get(request, store):
    return 'result', {'settings': store.settings}


def _settings_update(request, store):
    try:
        saved = store.update(request['payload'])
    except ValidationError as exc:
        raise ProtocolError('invalid_settings', str(exc), request['id']) from None
    except OSError:
        raise ProtocolError('save_failed', 'Settings were not saved; last successful values retained.',
                            request['id']) from None
    return 'result', {'settings': saved}


def _shutdown(request, store):
    return 'result', {}


def _cancel_reserved(request, store):
    return 'cancelled', {}


def _not_available(request, store):
    raise ProtocolError('not_available', 'This feature is reserved for a later stage.', request['id'])


HANDLERS = {
    'hello': _hello,
    'settings.get': _settings_get,
    'settings.update': _settings_update,
    'shutdown': _shutdown,
    'tts.cancel': _cancel_reserved,
    **chat.COMMANDS,
}


def _is_reserved(command):
    return command in RESERVED_COMMANDS or command == RESERVED_PREFIX \
        or command.startswith(RESERVED_PREFIX + '.')


def dispatch(request, store):
    handler = HANDLERS.get(request['type'])
    if handler is not None:
        return handler(request, store)
    if _is_reserved(request['type']):
        return _not_available(request, store)
    raise ProtocolError('unknown_command', 'Unknown command.', request['id'])
