"""Synchronous request dispatcher: one owner, no background settings writes."""

from contextlib import ExitStack
import sqlite3

from service import PROTOCOL_VERSION, SERVICE_VERSION
from service.database import Database
from service.locking import AlreadyRunning, DataDirectoryLock
from service.protocol import ProtocolError, decode, emit, emit_error, frames
from service.settings import SettingsStore, ValidationError


class BoundedLog:
    def __init__(self, stream):
        self.stream = stream
        self.remaining = 8192

    def write(self, message):
        data = (message[:240] + '\n').encode('utf-8', errors='replace')[:self.remaining]
        if not data:
            return
        self.remaining -= len(data)
        try:
            self.stream.write(data)
            self.stream.flush()
        except OSError:
            pass


def dispatch(request, store):
    command, payload, request_id = request['type'], request['payload'], request['id']
    if command == 'hello':
        if type(payload.get('protocol_version')) is not int:
            raise ProtocolError('invalid_request', 'hello requires integer protocol_version.', request_id)
        if payload['protocol_version'] != PROTOCOL_VERSION:
            raise ProtocolError('unsupported_version', 'Only protocol version 1 is supported.', request_id)
        return 'ready', {'protocol_version': PROTOCOL_VERSION, 'service_version': SERVICE_VERSION,
                         'settings': store.settings, 'warnings': store.warnings}
    if command == 'settings.get':
        return 'result', {'settings': store.settings}
    if command == 'settings.update':
        try:
            saved = store.update(payload)
        except ValidationError as exc:
            raise ProtocolError('invalid_settings', str(exc), request_id) from None
        except OSError:
            raise ProtocolError('save_failed', 'Settings were not saved; last successful values retained.',
                                request_id) from None
        return 'result', {'settings': saved}
    if command in ('chat.cancel', 'tts.cancel'):
        return 'cancelled', {}
    if command == 'shutdown':
        return 'result', {}
    if command in ('chat.send', 'tts.synthesize') or command == 'memory' or command.startswith('memory.'):
        raise ProtocolError('not_available', 'This feature is not available in stage 1.', request_id)
    raise ProtocolError('unknown_command', 'Unknown command.', request_id)


def serve(directory, input_stream, output_stream, error_stream):
    log = BoundedLog(error_stream)
    with ExitStack() as resources:
        try:
            resources.enter_context(DataDirectoryLock(directory))
            database = Database(directory / 'pet.db')
            resources.callback(database.close)
            store = SettingsStore(directory)
        except AlreadyRunning:
            emit_error(output_stream, ProtocolError('already_running', 'Data directory already has a service.'))
            return 1
        except (OSError, sqlite3.Error):
            log.write('Service storage initialization failed.')
            emit_error(output_stream, ProtocolError('storage_unavailable', 'Cannot initialize service storage.'))
            return 1
        for warning in store.warnings:
            log.write(warning)
        try:
            for raw in frames(input_stream):
                try:
                    request = decode(raw)
                    event, payload = dispatch(request, store)
                    emit(output_stream, request['id'], event, payload)
                    if request['type'] == 'shutdown':
                        return 0
                except ProtocolError as exc:
                    emit_error(output_stream, exc)
                    if exc.code == 'save_failed':
                        log.write('Settings save failed; retained last successful values.')
        except ProtocolError as exc:
            emit_error(output_stream, exc)
            return 2 if exc.fatal else 0
    return 0
