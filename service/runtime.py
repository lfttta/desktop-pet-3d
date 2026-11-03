"""Synchronous request dispatcher: one owner, no background settings writes."""

from contextlib import ExitStack
import sqlite3

from service.commands import dispatch
from service.database import Database
from service.locking import AlreadyRunning, DataDirectoryLock
from service.protocol import ProtocolError, decode, emit, emit_error, frames
from service.settings import SettingsStore


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
