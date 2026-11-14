"""Voice output command.

``tts.synthesize`` validates the line against the ``voice`` settings group and
hands it to the runtime's speech worker; it answers at once with a synthesis
request id. The rendered WAV arrives later as a ``tts.ready`` event (or
``tts.failed`` when the backend cannot speak) posted under that id, so a slow
render never blocks the service loop, other requests or chat streaming.
"""

import queue
import uuid
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
ID_PREFIX = 'tts-'


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
        prepared = tts.prepare(text, voice=voice,
                               rate=config.get('rate', DEFAULT_RATE),
                               volume=config.get('volume', DEFAULT_VOLUME))
    except ProtocolError as exc:
        raise ProtocolError(exc.code, str(exc), request['id']) from None
    # Only the real runtime context carries the worker; a context-less dispatch
    # (unit tests, tooling) has nowhere to render and says so explicitly.
    speech = getattr(ctx, 'speech', None)
    if speech is None:
        raise ProtocolError('tts_unavailable',
                            'Speech synthesis is unavailable.', request['id'])
    request_id = ID_PREFIX + uuid.uuid4().hex
    try:
        speech.submit({'id': request_id, **prepared})
    except queue.Full:
        raise ProtocolError('tts_busy', 'Too many lines are waiting to be spoken.',
                            request['id']) from None
    except RuntimeError:
        raise ProtocolError('tts_unavailable',
                            'Speech synthesis is unavailable.', request['id']) from None
    return 'result', {'request_id': request_id, 'accepted': True}


COMMANDS = {'tts.synthesize': synthesize}
