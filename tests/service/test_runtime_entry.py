"""Entry point and runtime housekeeping: the paths a real launch takes.

``service/main.py`` is the console entry PyInstaller and Godot execute, so its
argument handling and exit codes are part of the product contract. The runtime
tests cover the error branches a healthy service never reaches: a failed
reader, a fatal protocol error, an exhausted log budget.
"""

import io
import threading

import pytest

from service import main as entry
from service.protocol import ProtocolError
from service.runtime import BoundedLog, EventLog, Runtime


class _FakeStdout:
    """A stand-in for sys.stdout that keeps a bytes buffer and records close()."""

    def __init__(self):
        self.buffer = io.BytesIO()
        self.closed = False

    def close(self):
        self.closed = True


# -- service/main.py -------------------------------------------------------


def test_main_reports_bad_arguments_without_starting_the_service(monkeypatch):
    calls = []
    monkeypatch.setattr(entry, 'serve', lambda *args: calls.append(args) or 0)
    stdout = _FakeStdout()
    monkeypatch.setattr(entry.sys, 'stdout', stdout)

    code = entry.main(['--unknown-flag'])

    assert code == 2
    assert calls == [], 'a bad argument list must not start the service'
    assert b'invalid_arguments' in stdout.buffer.getvalue()


def test_main_defaults_the_data_directory_to_localappdata(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(entry, 'serve',
                        lambda directory, *rest: seen.append(directory) or 0)
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    monkeypatch.setattr(entry.sys, 'stdout', _FakeStdout())

    assert entry.main([]) == 0
    assert seen == [tmp_path / 'Pet3D']


def test_main_honours_an_explicit_data_directory(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(entry, 'serve',
                        lambda directory, *rest: seen.append(directory) or 0)
    monkeypatch.setattr(entry.sys, 'stdout', _FakeStdout())

    assert entry.main(['--data-dir', str(tmp_path)]) == 0
    assert seen == [tmp_path]


def test_main_swallows_broken_pipe_when_the_client_disappears(monkeypatch):
    def explode(*_args):
        raise BrokenPipeError()

    stdout = _FakeStdout()
    monkeypatch.setattr(entry, 'serve', explode)
    monkeypatch.setattr(entry.sys, 'stdout', stdout)

    assert entry.main(['--data-dir', 'ignored']) == 0
    assert stdout.closed is True, 'the stream is closed so shutdown cannot flush it again'


# -- runtime error branches ------------------------------------------------


def test_event_log_trims_to_the_limit_and_clears():
    log = EventLog(maxlen=3)
    for index in range(5):
        log.record('info', 'request', 'n%d' % index, 'message')

    assert [item['code'] for item in log.tail()] == ['n2', 'n3', 'n4']
    assert len(log) == 3
    assert log.tail(1)[0]['code'] == 'n4'

    log.clear()
    assert len(log) == 0
    assert log.tail() == []


def test_event_log_keeps_only_codes_and_short_messages():
    log = EventLog(maxlen=10)
    log.record('error', 'tick', 'hook_failed', 'x' * 5000)

    assert len(log.tail()[0]['message']) <= 240, 'the log must not carry an unbounded message'


def test_register_tick_rejects_a_non_callable():
    runtime = Runtime(store=object(), output_stream=io.BytesIO(), log=io.StringIO())

    with pytest.raises(TypeError):
        runtime.register_tick('not callable')


def test_emit_from_a_worker_thread_is_refused():
    runtime = Runtime(store=object(), output_stream=io.BytesIO(), log=io.StringIO())
    runtime._owner = threading.get_ident()
    failure = []

    def worker():
        try:
            runtime.emit('r1', 'result', {})
        except RuntimeError as exc:
            failure.append(str(exc))

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()

    assert failure and 'post()' in failure[0]


def test_a_reader_failure_stops_the_service_with_exit_code_one():
    runtime = Runtime(store=object(), output_stream=io.BytesIO(), log=io.StringIO())
    runtime._owner = threading.get_ident()

    runtime._handle(('reader_error', None))

    assert runtime.exit_code == 1
    assert runtime._stop_requested is True
    assert any(item['code'] == 'reader_failed' for item in runtime.events.tail())


def test_a_fatal_protocol_error_sets_exit_code_two():
    runtime = Runtime(store=object(), output_stream=io.BytesIO(), log=io.StringIO())
    runtime._owner = threading.get_ident()
    error = ProtocolError('bad_frame', 'frame too large', fatal=True)

    runtime._handle(('error', error))

    assert runtime.exit_code == 2
    assert runtime._stop_requested is True


def test_a_non_fatal_protocol_error_keeps_the_service_alive():
    runtime = Runtime(store=object(), output_stream=io.BytesIO(), log=io.StringIO())
    runtime._owner = threading.get_ident()
    runtime._handle(('error', ProtocolError('invalid_request', 'missing payload')))

    assert runtime.exit_code == 0
    assert runtime._stop_requested is False
    assert any(item['code'] == 'invalid_request' for item in runtime.events.tail())


def test_eof_stops_the_service_without_an_error():
    runtime = Runtime(store=object(), output_stream=io.BytesIO(), log=io.StringIO())
    runtime._owner = threading.get_ident()

    runtime._handle(('eof', None))

    assert runtime._stop_requested is True
    assert runtime.exit_code == 0


def test_bounded_log_stops_writing_once_its_budget_is_spent():
    stream = io.BytesIO()
    log = BoundedLog(stream)

    for _ in range(200):
        log.write('x' * 240)
    written = stream.getvalue()

    assert 0 < len(written) <= 8192, 'the log must stay inside its 8 KiB page'
    log.write('after the budget')
    assert len(stream.getvalue()) == len(written), 'a spent log writes nothing more'


def test_bounded_log_survives_a_closed_stream():
    class _Broken:
        def write(self, _data):
            raise OSError('pipe closed')

        def flush(self):
            raise OSError('pipe closed')

    log = BoundedLog(_Broken())
    log.write('anything')  # must not raise
