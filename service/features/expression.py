"""Expression phrase management commands.

The phrase library is read from ``data/expression/phrases.json`` and user
toggles are stored under the data directory in ``expression/phrases.local.json``.
Listing never mutates state; enabling or disabling one phrase rewrites that
override file atomically.
"""

from pathlib import Path
import re

from service.expression import phrases
from service.protocol import ProtocolError

NAME = 'expression'
CAPABILITY = {
    'available': True,
    'reason': '',
    'commands': ['expression.phrases.list', 'expression.phrases.set_enabled'],
}
PHRASE_ID_PATTERN = re.compile(r'^[A-Za-z0-9_-]{1,128}$')
PHRASE_FIELDS = ('id', 'text', 'category', 'enabled', 'cooldown_seconds',
                 'daily_limit', 'source')


def _data_dir(store):
    path = getattr(store, 'path', None)
    if path is not None:
        return Path(path).parent
    directory = getattr(store, 'directory', None)
    return Path(directory) if directory is not None else None


def _phrase_id(payload, request_id):
    value = payload.get('phrase_id')
    if type(value) is not str or PHRASE_ID_PATTERN.match(value) is None:
        raise ProtocolError('invalid_request',
                            'phrase_id must be a local phrase identifier.',
                            request_id)
    return value


def _enabled(payload, request_id):
    value = payload.get('enabled')
    if type(value) is not bool:
        raise ProtocolError('invalid_request', 'enabled must be a boolean.',
                            request_id)
    return value


def list_phrases(request, store, ctx):
    try:
        items = phrases.list_phrases(data_dir=_data_dir(store))
    except (OSError, ValueError, UnicodeError):
        raise ProtocolError('storage_unavailable',
                            'The phrase library could not be read.',
                            request['id']) from None
    result = [{key: item.get(key) for key in PHRASE_FIELDS} for item in items]
    return 'result', {'phrases': result, 'count': len(result)}


def set_enabled(request, store, ctx):
    payload = request['payload']
    phrase_id = _phrase_id(payload, request['id'])
    enabled = _enabled(payload, request['id'])
    try:
        updated = phrases.set_enabled(phrase_id, enabled, data_dir=_data_dir(store))
    except KeyError:
        raise ProtocolError('invalid_request',
                            'phrase_id is not in the phrase library.',
                            request['id']) from None
    except (OSError, ValueError, UnicodeError):
        raise ProtocolError('save_failed',
                            'The phrase preference was not saved.',
                            request['id']) from None
    return 'result', {'saved': True, 'phrase_id': updated['id'],
                      'enabled': updated['enabled']}


COMMANDS = {
    'expression.phrases.list': list_phrases,
    'expression.phrases.set_enabled': set_enabled,
}
