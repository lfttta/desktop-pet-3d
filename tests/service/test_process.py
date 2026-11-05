"""Identical live pipe contracts for Python source and the frozen executable."""

import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading

import pytest

ROOT = Path(__file__).resolve().parents[2]
LIMIT = 1024 * 1024


def frame(command, payload=None, request_id='请求一', version=1):
    return (json.dumps({'version': version, 'id': request_id, 'type': command,
                        'payload': {} if payload is None else payload},
                       ensure_ascii=False, separators=(',', ':')) + '\n').encode('utf-8')


class Client:
    def __init__(self, command, cwd, data_dir, env):
        arguments = command + (['--data-dir', str(data_dir)] if data_dir is not None else [])
        self.process = subprocess.Popen(arguments, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, cwd=cwd, env=env)
        self.lines = queue.Queue()
        self.errors = bytearray()
        self.stdout_thread = threading.Thread(target=self._read, daemon=True)
        self.stderr_thread = threading.Thread(target=self._errors, daemon=True)
        self.stdout_thread.start()
        self.stderr_thread.start()

    def _read(self):
        for line in self.process.stdout:
            self.lines.put(line)
        self.lines.put(None)

    def _errors(self):
        while chunk := self.process.stderr.read(4096):
            self.errors.extend(chunk)

    def write(self, raw):
        self.process.stdin.write(raw)
        self.process.stdin.flush()

    def receive(self):
        raw = self.lines.get(timeout=15)
        assert raw is not None, f'process exited before reply: {self.errors!r}'
        assert len(raw) <= LIMIT + 1 and raw.endswith(b'\n')
        reply = json.loads(raw.decode('utf-8'))
        assert set(reply) == {'version', 'id', 'type', 'payload'}
        assert type(reply['version']) is int and reply['version'] == 1
        assert type(reply['id']) is str and type(reply['payload']) is dict
        return reply

    def request(self, command, payload=None, request_id='请求一', version=1):
        self.write(frame(command, payload, request_id, version))
        reply = self.receive()
        assert reply['id'] == request_id
        return reply

    def finish(self, expected=0):
        self.process.stdin.close()
        assert self.process.wait(timeout=15) == expected
        self.stdout_thread.join(timeout=2)
        self.stderr_thread.join(timeout=2)
        assert self.lines.get(timeout=2) is None, 'unexpected extra stdout'
        assert len(self.errors) <= 8192

    def cleanup(self):
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(timeout=10)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            try:
                stream.close()
            except (OSError, ValueError):
                pass


@pytest.fixture
def launch(tmp_path, request):
    executable = request.config.getoption('--service-exe')
    command = [str(Path(executable).resolve())] if executable else [sys.executable, '-u', str(ROOT / 'service/main.py')]
    env = os.environ.copy()
    env.pop('PYTHONHOME', None)
    env.pop('PYTHONPATH', None)
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    # Binary protocol must survive Windows text stdio defaults, including emoji.
    env['PYTHONIOENCODING'] = 'cp936:strict'
    env['LOCALAPPDATA'] = str(tmp_path / 'local app data')
    if executable:
        env['PATH'] = str(Path(os.environ.get('SystemRoot', 'C:/Windows')) / 'System32')
    clients = []

    def create(data_dir=...):
        chosen = tmp_path / '用户 数据' if data_dir is ... else data_dir
        client = Client(command, tmp_path, chosen, env)
        clients.append(client)
        return client

    yield create
    for client in clients:
        client.cleanup()


def test_fragmented_utf8_multiple_lines_persist_cancel_and_shutdown(launch, tmp_path):
    client = launch()
    with pytest.raises(queue.Empty):
        client.lines.get(timeout=0.15)
    hello = frame('hello', {'protocol_version': 1})
    split = hello.index('请'.encode('utf-8')) + 1
    client.write(hello[:split])
    with pytest.raises(queue.Empty):
        client.lines.get(timeout=0.15)
    client.write(hello[split:])
    ready = client.receive()
    assert ready['type'] == 'ready' and ready['id'] == '请求一'
    assert ready['payload']['protocol_version'] == 1
    assert ready['payload']['service_version']
    assert ready['payload']['warnings'] == []
    assert ready['payload']['settings']['user_name'] == '你'
    patch = {'user_name': '小明 🐾', 'character_id': 'godette', 'scale': 1.75,
             'screen_id': '显示器-二',
             'placement': {'x': 0.25, 'y': 0.5, 'edge_x': -1, 'edge_y': 0}}
    client.write(frame('settings.update', patch, 'save') + frame('chat.cancel', {}, 'c')
                 + frame('tts.cancel', {}, 't') + frame('settings.get', {}, 'get')
                 + frame('shutdown', {}, 'stop') + frame('settings.update', {'scale': 0.5}, 'ignored'))
    saved = client.receive()
    assert saved['type'] == 'result' and saved['id'] == 'save'
    settings = saved['payload']['settings']
    assert all(settings[k] == v for k, v in patch.items())
    assert json.loads((tmp_path / '用户 数据/settings.json').read_text('utf-8')) == settings
    assert client.receive()['type'] == 'cancelled'
    tts = client.receive()
    assert tts['type'] == 'error' and tts['payload']['code'] == 'not_available'
    assert client.receive()['payload']['settings'] == settings
    shutdown = client.receive()
    assert shutdown['type'] == 'result' and shutdown['id'] == 'stop'
    client.finish()
    restarted = launch()
    assert restarted.request('hello', {'protocol_version': 1})['payload']['settings'] == settings
    restarted.finish()


@pytest.mark.parametrize('command', ['tts.synthesize', 'memory.update', 'user.profile'])
def test_unavailable_features_are_explicit_errors(launch, command):
    client = launch()
    reply = client.request(command, {'text': 'hello'})
    assert reply['type'] == 'error' and reply['payload']['code'] == 'not_available'
    client.finish()


@pytest.mark.parametrize('raw,code', [
    (b'{bad}\n', 'invalid_json'), (b'\xff\n', 'invalid_json'), (b'\n', 'invalid_json'),
    (b'[]\n', 'invalid_request'),
    (b'{"version":1,"id":2,"type":"hello","payload":{}}\n', 'invalid_request'),
    (b'{"version":true,"id":"x","type":"hello","payload":{}}\n', 'invalid_request'),
    (b'{"version":1,"id":"x","type":3,"payload":{}}\n', 'invalid_request'),
    (b'{"version":1,"id":"x","type":"hello","payload":[]}\n', 'invalid_request'),
    (b'{"version":1,"id":"x","type":"hello"}\n', 'invalid_request'),
    (b'{"version":1,"id":"x","type":"hello","payload":{},"extra":1}\n', 'invalid_request'),
    (b'{"version":1,"version":2,"id":"x","type":"hello","payload":{}}\n', 'invalid_json'),
    (b'{"version":1,"id":"x","type":"settings.update","payload":{"scale":NaN}}\n', 'invalid_json'),
    (b'{"version":1,"id":"\\ud800","type":"hello","payload":{}}\n', 'invalid_request'),
    pytest.param(b'[' * 2000 + b']' * 2000 + b'\n', 'invalid_json', id='deep-nesting'),
    pytest.param(b'{"version":1,"id":"x","type":"hello","payload":{"value":1e400}}\n',
                 'invalid_json', id='overflow-number'),
])
def test_malformed_line_resynchronizes_without_losing_next_request(launch, raw, code):
    client = launch()
    client.write(raw + frame('settings.get', {}, 'next'))
    error = client.receive()
    assert error['type'] == 'error' and error['payload']['code'] == code
    assert error['id'] == ''
    assert len(json.dumps(error)) < 2048
    reply = client.receive()
    assert reply['id'] == 'next' and reply['type'] == 'result'
    client.finish()


@pytest.mark.parametrize('command,payload,version,code', [
    ('hello', {'protocol_version': 1}, 2, 'unsupported_version'),
    ('hello', {'protocol_version': 2}, 1, 'unsupported_version'),
    ('hello', {'protocol_version': True}, 1, 'invalid_request'),
    ('hello', {}, 1, 'invalid_request'),
    ('settings.update', {'scale': True}, 1, 'invalid_settings'),
    ('settings.update', {'api_key': 'secret'}, 1, 'invalid_settings'),
    ('nonsense', {}, 1, 'unknown_command'),
])
def test_versions_and_command_errors_preserve_id_and_continue(launch, command, payload, version, code):
    client = launch()
    reply = client.request(command, payload, version=version)
    assert reply['type'] == 'error' and reply['payload']['code'] == code
    assert client.request('settings.get')['payload']['settings']['scale'] == 1.0
    client.finish()


def test_oversize_frame_errors_then_exits_without_waiting_for_newline(launch):
    client = launch()
    try:
        client.write(b' ' * (LIMIT + 1))
    except BrokenPipeError:
        pass
    reply = client.receive()
    assert reply['type'] == 'error' and reply['payload']['code'] == 'frame_too_large'
    client.finish(expected=2)


def test_exact_limit_frame_and_crlf_are_accepted(launch):
    client = launch()
    command = frame('settings.get', {}, 'limit')[:-1]
    client.write(b' ' * (LIMIT - len(command)) + command + b'\n')
    assert client.receive()['id'] == 'limit'
    client.write(frame('settings.get', {}, 'crlf')[:-1] + b'\r\n')
    assert client.receive()['id'] == 'crlf'
    client.finish()


def test_unterminated_final_frame_does_not_mutate_settings(launch, tmp_path):
    client = launch()
    client.write(frame('settings.update', {'user_name': '不完整'})[:-1])
    client.process.stdin.close()
    reply = client.receive()
    assert reply['type'] == 'error' and reply['payload']['code'] == 'incomplete_frame'
    client.finish()
    assert json.loads((tmp_path / '用户 数据/settings.json').read_text('utf-8'))['user_name'] == '你'


def test_empty_eof_exits_and_default_localappdata_is_used(launch, tmp_path):
    client = launch(None)
    client.finish()
    assert (tmp_path / 'local app data/Pet3D/settings.json').is_file()
    assert (tmp_path / 'local app data/Pet3D/pet.db').is_file()


@pytest.mark.parametrize('raw', [b'\xffbad config', b'{"schema_version":77}'])
def test_real_process_bad_config_backup_and_restart(launch, tmp_path, raw):
    directory = tmp_path / '用户 数据'
    directory.mkdir()
    (directory / 'settings.json').write_bytes(raw)
    client = launch()
    ready = client.request('hello', {'protocol_version': 1})
    assert ready['type'] == 'ready' and ready['payload']['warnings']
    backups = list(directory.glob('settings.json.broken-*'))
    assert len(backups) == 1 and backups[0].read_bytes() == raw
    client.finish()
    restarted = launch()
    assert restarted.request('hello', {'protocol_version': 1})['payload']['warnings'] == []
    restarted.finish()


def test_real_save_failure_never_returns_success_and_cancellation_still_works(launch, tmp_path):
    client = launch()
    assert client.request('settings.update', {'user_name': '之前'})['type'] == 'result'
    path = tmp_path / '用户 数据/settings.json'
    old_bytes = path.read_bytes()
    # A directory at the destination is a deterministic real os.replace failure.
    path.rename(path.with_suffix('.held'))
    path.mkdir()
    reply = client.request('settings.update', {'user_name': '错误值'})
    assert reply['type'] == 'error' and reply['payload']['code'] == 'save_failed'
    assert client.request('chat.cancel')['type'] == 'cancelled'
    assert client.request('settings.get')['payload']['settings']['user_name'] == '之前'
    assert path.with_suffix('.held').read_bytes() == old_bytes
    path.rmdir()
    path.with_suffix('.held').rename(path)
    assert client.request('settings.update', {'user_name': '恢复'})['type'] == 'result'
    assert client.request('shutdown')['type'] == 'result'
    client.finish()
    assert json.loads(path.read_text('utf-8'))['user_name'] == '恢复'


def test_second_writer_fails_cleanly_and_lock_is_released(launch):
    first = launch()
    assert first.request('hello', {'protocol_version': 1})['type'] == 'ready'
    second = launch()
    reply = second.receive()
    assert reply['type'] == 'error' and reply['payload']['code'] == 'already_running'
    second.finish(expected=1)
    first.finish()
    third = launch()
    assert third.request('settings.get')['type'] == 'result'
    third.finish()


def test_unwritable_data_path_is_bounded_explicit_startup_error(launch, tmp_path):
    path = tmp_path / 'file-not-directory'
    path.write_bytes(b'keep')
    client = launch(path)
    error = client.receive()
    assert error['type'] == 'error' and error['payload']['code'] == 'storage_unavailable'
    client.finish(expected=1)
    assert path.read_bytes() == b'keep'


def test_invalid_command_flood_has_bounded_stderr(launch):
    client = launch()
    for index in range(200):
        assert client.request('unknown', request_id=str(index))['type'] == 'error'
    client.finish()
