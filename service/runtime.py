"""Event loop: blocking stdin lives on a reader thread, writes stay on the owner.

The reader thread only frames bytes into an inbound queue. Every decode,
handler call, SQLite write and stdout frame happens on the main thread. Handlers
receive a RequestContext whose emit() writes one frame immediately; deferred
frames posted from any thread are drained once per loop iteration. A 100 ms tick
hook gives later async features (streaming, autonomy) a heartbeat without
blocking the request queue.
"""

from collections import deque
from contextlib import ExitStack
from datetime import datetime, timezone
import queue
import sqlite3
import threading
import time

from service.commands import dispatch
from service.database import Database
from service.locking import AlreadyRunning, DataDirectoryLock
from service.protocol import ProtocolError, decode, frames, emit as emit_frame, emit_error
from service.settings import SettingsStore

TICK_SECONDS = 0.1
EVENT_LOG_MAXLEN = 500
EVENT_LOG_LIMIT = 50
MAX_LEVEL_CHARS = 16
MAX_MODULE_CHARS = 64
MAX_CODE_CHARS = 64
MAX_MESSAGE_CHARS = 240


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


class EventLog:
    """Thread-safe, bounded buffer of operational events.

    Callers record event codes, module names and short diagnostic messages
    only. User text, prompts and credentials must never be passed here; the
    service uses this buffer for diagnostics, not for transcript storage.
    """

    def __init__(self, maxlen=EVENT_LOG_MAXLEN):
        self.maxlen = max(1, int(maxlen))
        self._entries = deque(maxlen=self.maxlen)
        self._lock = threading.Lock()

    @staticmethod
    def _text(value, limit):
        text = '' if value is None else str(value)
        return text.replace('\r', ' ').replace('\n', ' ')[:limit]

    def record(self, level, module, code, message=''):
        entry = {
            'time': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ'),
            'level': self._text(level, MAX_LEVEL_CHARS) or 'info',
            'module': self._text(module, MAX_MODULE_CHARS),
            'code': self._text(code, MAX_CODE_CHARS),
            'message': self._text(message, MAX_MESSAGE_CHARS),
        }
        with self._lock:
            self._entries.append(entry)

    def tail(self, limit=EVENT_LOG_LIMIT):
        if type(limit) is not int or limit < 0:
            limit = 0
        limit = min(limit, self.maxlen)
        if limit == 0:
            return []
        with self._lock:
            entries = list(self._entries)[-limit:]
        return [dict(entry) for entry in entries]

    def clear(self):
        with self._lock:
            self._entries.clear()

    def __len__(self):
        with self._lock:
            return len(self._entries)


class RequestContext:
    """Per-request handle passed to handlers as the third argument."""

    def __init__(self, runtime, request):
        self.runtime = runtime
        self.request = request
        self.request_id = request['id']
        self.store = runtime.store
        self.database = runtime.database
        self.log = runtime.events

    def emit(self, event, payload):
        self.runtime.emit(self.request_id, event, payload)

    def post(self, event, payload):
        self.runtime.post(self.request_id, event, payload)

    def register_tick(self, hook):
        self.runtime.register_tick(hook)


class Runtime:
    def __init__(self, store, output_stream, log, database=None, tick_seconds=TICK_SECONDS,
                 event_log=None):
        self.store = store
        self.database = database
        self.output_stream = output_stream
        self.log = log
        self.events = event_log if event_log is not None else EventLog()
        self.tick_seconds = tick_seconds
        self.inbound = queue.Queue()
        self.outbound = queue.Queue()
        self.exit_code = 0
        self._owner = None
        self._stop_requested = False
        self._tick_hooks = []
        self._last_tick = time.monotonic()

    def register_tick(self, hook):
        if not callable(hook):
            raise TypeError('tick hook must be callable')
        self._tick_hooks.append(hook)

    def emit(self, request_id, event, payload):
        if self._owner is not None and threading.get_ident() != self._owner:
            raise RuntimeError('frames must be emitted on the service thread; use post()')
        emit_frame(self.output_stream, request_id, event, payload)

    def post(self, request_id, event, payload):
        self.outbound.put((request_id, event, payload))

    def _drain_outbound(self):
        while True:
            try:
                request_id, event, payload = self.outbound.get_nowait()
            except queue.Empty:
                return
            emit_frame(self.output_stream, request_id, event, payload)

    def _run_ticks(self):
        now = time.monotonic()
        if now - self._last_tick < self.tick_seconds:
            return
        self._last_tick = now
        for hook in tuple(self._tick_hooks):
            try:
                hook(now)
            except ProtocolError as exc:
                self.events.record('error', 'tick', exc.code, str(exc))
                emit_error(self.output_stream, exc)
            except Exception:
                self.events.record('error', 'tick', 'hook_failed', 'Tick hook failed.')
                self.log.write('Tick hook failed.')

    def _read_loop(self, input_stream):
        try:
            for raw in frames(input_stream):
                self.inbound.put(('frame', raw))
        except ProtocolError as exc:
            self.inbound.put(('error', exc))
        except Exception:
            self.inbound.put(('reader_error', None))
        finally:
            self.inbound.put(('eof', None))

    def _handle(self, item):
        kind = item[0]
        if kind == 'eof':
            self._stop_requested = True
            return
        if kind == 'reader_error':
            self.events.record('error', 'runtime', 'reader_failed', 'Input reader failed.')
            self.log.write('Input reader failed.')
            self.exit_code = 1
            self._stop_requested = True
            return
        if kind == 'error':
            error = item[1]
            self.events.record('error', 'protocol', error.code, str(error))
            emit_error(self.output_stream, error)
            if error.code == 'save_failed':
                self.log.write('Settings save failed; retained last successful values.')
            if error.fatal:
                self.exit_code = 2
                self._stop_requested = True
            return
        raw = item[1]
        request = None
        try:
            request = decode(raw)
            context = RequestContext(self, request)
            result = dispatch(request, self.store, context)
            if result is not None:
                event, payload = result
                emit_frame(self.output_stream, request['id'], event, payload)
            self.events.record('info', 'request', request['type'], 'handled')
        except ProtocolError as exc:
            self.events.record('error', 'protocol', exc.code, str(exc))
            emit_error(self.output_stream, exc)
            if exc.code == 'save_failed':
                self.log.write('Settings save failed; retained last successful values.')
        if request is not None and request['type'] == 'shutdown':
            self._stop_requested = True

    def run(self, input_stream):
        self._owner = threading.get_ident()
        reader = threading.Thread(target=self._read_loop, args=(input_stream,),
                                  name='service-reader', daemon=True)
        reader.start()
        try:
            while not self._stop_requested:
                self._drain_outbound()
                self._run_ticks()
                try:
                    item = self.inbound.get(timeout=self.tick_seconds)
                except queue.Empty:
                    continue
                self._handle(item)
                while not self._stop_requested:
                    try:
                        pending = self.inbound.get_nowait()
                    except queue.Empty:
                        break
                    self._handle(pending)
                self._drain_outbound()
        finally:
            reader.join(timeout=0.2)
        return self.exit_code


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
        runtime = Runtime(store, output_stream, log, database)
        # Service-initiated tick hooks (autonomy) need the runtime, so they are
        # attached after construction and before the loop starts.
        store.log = log
        from service.features import autonomy
        autonomy.register_tick(runtime, store, database)
        try:
            return runtime.run(input_stream)
        except ProtocolError as exc:
            emit_error(output_stream, exc)
            return 2 if exc.fatal else 0
