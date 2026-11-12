"""Voice output command.

``tts.synthesize`` renders one short line through the Windows speech backend.
The request only carries the text and an optional voice override; rate, volume
and the default voice come from the ``voice`` settings group so the app does not
have to duplicate them on every call.
"""

from pathlib import Path

from service import tts
from service.protocol import ProtocolError

NAME = 'voice'
CAPABILITY = {
    'available': True,
    'reason': '',
    'commands': ['tts.synthesize'],
}
DEFAULT_RATE = 0
DEFAULT_VOLUME = 70


def _data_dir(store):
    path = getattr(store, 'path', None)
    if path is not None:
        return Path(path).parent
    directory = getattr(store, 'directory', None)
    return Path(directory) if directory is not None else None


def _voice_settings(store):
    settings = getattr(store, 'settings', None)
    if isinstance(settings, dict) and isinstance(settings.get('voice'), dict):
        return settings['voice']
    return {}


def synthesize(request, store, ctx):
    payload = request['payload']
    text = payload.get('text')
    if type(text) is not str or not text.strip():
        raise ProtocolError('invalid_request',
                            'tts.synthesize requires non-empty text.',
                            request['id'])
    config = _voice_settings(store)
    # `voice.enabled` is the user's switch for spoken replies. Reported as an
    # error rather than an empty result so a silenced pet stays distinguishable
    # from a synthesizer that produced nothing.
    if not config.get('enabled', True):
        raise ProtocolError('voice_disabled',
                            'Speaking is turned off in settings.',
                            request['id'])
    voice = payload.get('voice', config.get('voice', ''))
    if type(voice) is not str:
        raise ProtocolError('invalid_request', 'voice must be a string.',
                            request['id'])
    data_dir = _data_dir(store)
    if data_dir is None:
        raise ProtocolError('storage_unavailable',
                            'The data directory is not available.',
                            request['id'])
    try:
        result = tts.synthesize(text, data_dir, voice=voice,
                                rate=config.get('rate', DEFAULT_RATE),
                                volume=config.get('volume', DEFAULT_VOLUME))
    except ProtocolError as exc:
        raise ProtocolError(exc.code, str(exc), request['id']) from None
    return 'result', result


COMMANDS = {'tts.synthesize': synthesize}
