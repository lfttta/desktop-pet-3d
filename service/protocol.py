"""Bounded UTF-8 JSON-lines framing; stdout is exclusively protocol traffic."""

from service import PROTOCOL_VERSION
from service.jsonutil import MAX_FRAME_BYTES, dumps, loads


class ProtocolError(Exception):
    def __init__(self, code, message, request_id='', fatal=False):
        super().__init__(message)
        self.code = code
        self.request_id = request_id
        self.fatal = fatal


def frames(stream):
    pending = bytearray()
    read = getattr(stream, 'read1', stream.read)
    while chunk := read(64 * 1024):
        pending.extend(chunk)
        start = 0
        while True:
            end = pending.find(b'\n', start)
            if end == -1:
                break
            if end - start > MAX_FRAME_BYTES:
                raise ProtocolError('frame_too_large', 'Frame exceeds 1 MiB; closing stream.', fatal=True)
            yield bytes(pending[start:end])
            start = end + 1
        if start:
            del pending[:start]
        if len(pending) > MAX_FRAME_BYTES:
            raise ProtocolError('frame_too_large', 'Frame exceeds 1 MiB; closing stream.', fatal=True)
    if pending:
        raise ProtocolError('incomplete_frame', 'EOF before newline; command discarded.')


def _valid_text(value, byte_limit):
    if type(value) is not str:
        return False
    try:
        return len(value.encode('utf-8')) <= byte_limit
    except UnicodeError:
        return False


def decode(raw):
    try:
        request = loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        raise ProtocolError('invalid_json', 'Expected one strict UTF-8 JSON object.') from None
    if (type(request) is not dict or request.keys() != {'version', 'id', 'type', 'payload'}
            or type(request.get('version')) is not int
            or not _valid_text(request.get('id'), 256)
            or not _valid_text(request.get('type'), 128)
            or not request['type'] or type(request.get('payload')) is not dict):
        raise ProtocolError('invalid_request', 'Invalid envelope or field types.')
    if request['version'] != PROTOCOL_VERSION:
        raise ProtocolError('unsupported_version', 'Only protocol version 1 is supported.', request['id'])
    return request


def emit(stream, request_id, event, payload):
    encoded = dumps({'version': PROTOCOL_VERSION, 'id': request_id, 'type': event, 'payload': payload})
    if len(encoded) > MAX_FRAME_BYTES:
        raise ProtocolError('response_too_large', 'Response exceeds 1 MiB.', request_id)
    stream.write(encoded + b'\n')
    stream.flush()


def emit_error(stream, error):
    emit(stream, error.request_id, 'error', {'code': error.code, 'message': str(error)})
