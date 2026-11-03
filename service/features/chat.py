"""Chat command contract placeholders.

Only validation and the transport envelope are implemented. Generation,
streaming and real cancellation belong to the later chat feature; until then a
valid request returns not_available so callers cannot mistake a stub for a
working assistant.
"""

from service.protocol import ProtocolError

MAX_TEXT_LENGTH = 8000
MAX_CONVERSATION_ID_LENGTH = 128


def _validate(payload, request_id):
    text = payload.get('text')
    if type(text) is not str or not text.strip():
        raise ProtocolError('invalid_request', 'chat.send requires non-empty text.', request_id)
    if len(text) > MAX_TEXT_LENGTH:
        raise ProtocolError('invalid_request', 'chat.send text exceeds 8000 characters.', request_id)
    conversation_id = payload.get('conversation_id', '')
    if type(conversation_id) is not str or len(conversation_id) > MAX_CONVERSATION_ID_LENGTH:
        raise ProtocolError('invalid_request', 'conversation_id must be a string of at most 128 characters.',
                            request_id)
    return text, conversation_id


def send(request, store):
    _validate(request['payload'], request['id'])
    raise ProtocolError('not_available', 'Chat is not implemented in this framework build.', request['id'])


def cancel(request, store):
    return 'cancelled', {}


COMMANDS = {'chat.send': send, 'chat.cancel': cancel}
