"""Speech synthesis contracts: cache, validation, failure mapping and clamps.

The Windows backend is always replaced here; these tests never start PowerShell
or open an audio device. The async path is covered with a fake backend whose
slow render the test holds and releases, proving the service loop keeps
answering requests, ticks and chat deltas while a line is still rendering.
"""

from copy import deepcopy
import io
import json
from pathlib import Path
import queue
import subprocess
import threading
import time
from types import SimpleNamespace

import pytest

import service.tts as tts
from service import commands
from service.protocol import ProtocolError
from service.runtime import BoundedLog, Runtime, TtsWorker, serve
from service.settings import (DEFAULTS, DESKTOP_DEFAULTS, SCHEMA_VERSION, VOICE_DEFAULTS,
                              SettingsStore, ValidationError)

WAV_BYTES = b'RIFF\x24\x00\x00\x00WAVEfake'


def _fake_backend(calls, payload=WAV_BYTES):
    def run(text, destination, voice, rate, volume):
        calls.append({'text': text, 'voice': voice, 'rate': rate, 'volume': volume})
        Path(destination).write_bytes(payload)
    return run


def test_second_synthesis_hits_cache_and_skips_the_backend(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(tts, '_run_powershell', _fake_backend(calls))

    first = tts.synthesize('你好', tmp_path, voice='Microsoft Huihui')
    second = tts.synthesize('你好', tmp_path, voice='Microsoft Huihui')

    assert first['cached'] is False
    assert second['cached'] is True
    assert first['voice'] == second['voice'] == 'Microsoft Huihui'
    assert first['path'] == second['path']
    assert len(calls) == 1
    path = Path(first['path'])
    assert path.is_absolute()
    assert path.parent == (tmp_path / 'tts').resolve()
    assert path.read_bytes() == WAV_BYTES


@pytest.mark.parametrize('text', ['', '   ', '\n\t '])
def test_blank_text_is_invalid_request(tmp_path, monkeypatch, text):
    calls = []
    monkeypatch.setattr(tts, '_run_powershell', _fake_backend(calls))

    with pytest.raises(ProtocolError) as caught:
        tts.synthesize(text, tmp_path)

    assert caught.value.code == 'invalid_request'
    assert calls == []


@pytest.mark.parametrize('error', [
    RuntimeError('no speech engine'),
    FileNotFoundError('powershell.exe'),
    subprocess.TimeoutExpired(['powershell.exe'], 20),
    subprocess.CalledProcessError(1, ['powershell.exe']),
])
def test_backend_failure_maps_to_tts_unavailable(tmp_path, monkeypatch, error):
    def explode(_text, _destination, _voice, _rate, _volume):
        raise error

    monkeypatch.setattr(tts, '_run_powershell', explode)
    with pytest.raises(ProtocolError) as caught:
        tts.synthesize('你好', tmp_path)

    assert caught.value.code == 'tts_unavailable'
    assert not list((tmp_path / 'tts').glob('*'))


def test_text_longer_than_500_characters_is_truncated(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(tts, '_run_powershell', _fake_backend(calls))

    tts.synthesize('字' * 650, tmp_path)

    assert len(calls) == 1
    assert len(calls[0]['text']) == 500
    assert calls[0]['text'] == '字' * 500


def test_rate_and_volume_are_clamped_to_the_backend_limits(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(tts, '_run_powershell', _fake_backend(calls))

    tts.synthesize('低', tmp_path, rate=-99, volume=-5)
    tts.synthesize('高', tmp_path, rate=99, volume=250)

    assert calls == [
        {'text': '低', 'voice': '', 'rate': -10, 'volume': 0},
        {'text': '高', 'voice': '', 'rate': 10, 'volume': 100},
    ]


def test_prepare_validates_without_touching_the_backend(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(tts, '_run_powershell', _fake_backend(calls))

    prepared = tts.prepare('  你好  ', voice=' Microsoft Huihui ', rate=99, volume=250)
    assert prepared == {'text': '你好', 'voice': 'Microsoft Huihui',
                        'rate': 10, 'volume': 100}
    with pytest.raises(ProtocolError) as caught:
        tts.prepare('   ')
    assert caught.value.code == 'invalid_request'
    assert calls == []


# The command: accept at once, render later


class _FakeSpeech:
    def __init__(self):
        self.jobs = []

    def submit(self, job):
        self.jobs.append(job)


def test_voice_command_accepts_at_once_and_enqueues_the_settings_group(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(tts, '_run_powershell', _fake_backend(calls))
    store = SettingsStore(tmp_path)
    store.update({'voice': {'enabled': True, 'voice': 'Microsoft Huihui',
                            'rate': 4, 'volume': 55}})
    speech = _FakeSpeech()

    event, payload = commands.dispatch(
        {'type': 'tts.synthesize', 'id': 'v1', 'payload': {'text': '你好'}}, store,
        SimpleNamespace(speech=speech))

    assert event == 'result'
    assert payload['accepted'] is True
    assert payload['request_id'].startswith('tts-')
    assert speech.jobs == [{'id': payload['request_id'], 'text': '你好',
                            'voice': 'Microsoft Huihui', 'rate': 4, 'volume': 55}]
    assert calls == []


def test_synthesize_without_a_runtime_reports_tts_unavailable(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(tts, '_run_powershell', _fake_backend(calls))
    store = SettingsStore(tmp_path)

    with pytest.raises(ProtocolError) as caught:
        commands.dispatch(
            {'type': 'tts.synthesize', 'id': 'v2', 'payload': {'text': '你好'}}, store)

    assert caught.value.code == 'tts_unavailable'
    assert calls == []


def test_full_queue_rejects_without_touching_the_backend(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(tts, '_run_powershell', _fake_backend(calls))
    store = SettingsStore(tmp_path)

    class _Full:
        def submit(self, job):
            raise queue.Full

    with pytest.raises(ProtocolError) as caught:
        commands.dispatch({'type': 'tts.synthesize', 'id': 'v3', 'payload': {'text': '你好'}},
                          store, SimpleNamespace(speech=_Full()))

    assert caught.value.code == 'tts_busy'
    assert calls == []


def test_closed_queue_rejects_without_touching_the_backend(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(tts, '_run_powershell', _fake_backend(calls))
    store = SettingsStore(tmp_path)

    class _Closed:
        def submit(self, job):
            raise RuntimeError('speech queue is closed')

    with pytest.raises(ProtocolError) as caught:
        commands.dispatch({'type': 'tts.synthesize', 'id': 'v4', 'payload': {'text': '你好'}},
                          store, SimpleNamespace(speech=_Closed()))

    assert caught.value.code == 'tts_unavailable'
    assert calls == []


# The worker: bounded queue, events, failure isolation, shutdown


def test_queue_is_bounded(tmp_path):
    runtime = Runtime(SettingsStore(tmp_path), io.BytesIO(), BoundedLog(io.BytesIO()))
    worker = TtsWorker(runtime, tmp_path)
    job = {'text': '你好', 'voice': '', 'rate': 0, 'volume': 70}
    for index in range(worker.maxsize):
        worker.submit(dict(job, id='tts-%d' % index))
    with pytest.raises(queue.Full):
        worker.submit(dict(job, id='tts-overflow'))


def test_worker_posts_ready_and_survives_failures(tmp_path, monkeypatch):
    state = {'fail': True}

    def backend(text, destination, voice, rate, volume):
        if state['fail']:
            raise RuntimeError('no engine')
        Path(destination).write_bytes(WAV_BYTES)

    monkeypatch.setattr(tts, '_run_powershell', backend)
    runtime = Runtime(SettingsStore(tmp_path), io.BytesIO(), BoundedLog(io.BytesIO()))
    worker = TtsWorker(runtime, tmp_path)
    assert worker.start()
    try:
        worker.submit({'id': 'tts-bad', 'text': '坏', 'voice': '', 'rate': 0, 'volume': 70})
        assert runtime.outbound.get(timeout=5) == (
            'tts-bad', 'tts.failed', {'id': 'tts-bad',
                                      'message': 'Speech synthesis is unavailable.'})
        state['fail'] = False
        worker.submit({'id': 'tts-good', 'text': '好', 'voice': '', 'rate': 0, 'volume': 70})
        request_id, event, payload = runtime.outbound.get(timeout=5)
        assert (request_id, event) == ('tts-good', 'tts.ready')
        assert payload['id'] == 'tts-good'
        assert payload['cached'] is False
        assert payload['voice'] == ''
        assert Path(payload['path']).read_bytes() == WAV_BYTES
    finally:
        worker.close()
    assert not worker._worker.is_alive()
    with pytest.raises(RuntimeError):
        worker.submit({'id': 'tts-late', 'text': '晚', 'voice': '', 'rate': 0, 'volume': 70})


def test_second_identical_job_reuses_the_rendered_cache(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(tts, '_run_powershell', _fake_backend(calls))
    runtime = Runtime(SettingsStore(tmp_path), io.BytesIO(), BoundedLog(io.BytesIO()))
    worker = TtsWorker(runtime, tmp_path)
    assert worker.start()
    try:
        job = {'text': '你好', 'voice': 'Microsoft Huihui', 'rate': 0, 'volume': 70}
        worker.submit(dict(job, id='tts-a'))
        first = runtime.outbound.get(timeout=5)
        worker.submit(dict(job, id='tts-b'))
        second = runtime.outbound.get(timeout=5)
        assert first[1] == second[1] == 'tts.ready'
        assert first[2]['cached'] is False and second[2]['cached'] is True
        assert first[2]['path'] == second[2]['path']
        assert len(calls) == 1
    finally:
        worker.close()


# Async contract against the real loop


class _Tee:
    """Bytes sink the test thread may read while the service thread writes."""

    def __init__(self):
        self._buffer = io.BytesIO()
        self._lock = threading.Lock()

    def write(self, data):
        with self._lock:
            return self._buffer.write(data)

    def flush(self):
        pass

    def frames(self):
        with self._lock:
            raw = self._buffer.getvalue()
        return [json.loads(line) for line in raw.decode('utf-8').splitlines()]


class _Gate:
    """Framed input that stays open until the test calls finish()."""

    def __init__(self, chunks):
        self._chunks = list(chunks)
        self._done = threading.Event()

    def finish(self):
        self._done.set()

    def read1(self, _size):
        if self._chunks:
            return self._chunks.pop(0)
        self._done.wait(timeout=10)
        return b''

    def read(self, size):
        return self.read1(size)


def _frame(command, payload=None, request_id='req', version=1):
    return (json.dumps({'version': version, 'id': request_id, 'type': command,
                        'payload': {} if payload is None else payload},
                       ensure_ascii=False, separators=(',', ':')) + '\n').encode('utf-8')


def _wait(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_slow_render_does_not_block_requests_ticks_or_deltas(tmp_path, monkeypatch):
    release = threading.Event()
    rendering = threading.Event()
    calls = []

    def backend(text, destination, voice, rate, volume):
        calls.append(text)
        if text == '慢':
            rendering.set()
            assert release.wait(timeout=10), 'the slow render was never released'
        Path(destination).write_bytes(WAV_BYTES)

    monkeypatch.setattr(tts, '_run_powershell', backend)

    def streamed(_request, _store, ctx):
        ctx.emit('chat.delta', {'text': '第一段'})
        ctx.emit('chat.delta', {'text': '第二段'})
        return 'chat.done', {'message': {'content': '第一段第二段'}}

    monkeypatch.setitem(commands.HANDLERS, 'chat.stream_test', streamed)

    output = _Tee()
    runtime = Runtime(SettingsStore(tmp_path), output, BoundedLog(io.BytesIO()),
                      tick_seconds=0.02)
    worker = TtsWorker(runtime, tmp_path)
    assert worker.start()
    runtime.speech = worker
    ticks = []
    runtime.register_tick(lambda _now: ticks.append(_now))

    inp = _Gate([
        _frame('tts.synthesize', {'text': '慢'}, 't1'),
        _frame('chat.stream_test', {}, 'c1'),
        _frame('settings.get', {}, 'g1'),
        _frame('tts.synthesize', {'text': '慢'}, 't2'),
        _frame('tts.synthesize', {'text': '快'}, 't3'),
    ])
    code = {}
    thread = threading.Thread(target=lambda: code.update(value=runtime.run(inp)),
                              daemon=True)
    try:
        thread.start()
        assert _wait(rendering.is_set), 'the slow render never started'

        def answered():
            pairs = [(item['id'], item['type']) for item in output.frames()]
            return pairs == [
                ('t1', 'result'), ('c1', 'chat.delta'), ('c1', 'chat.delta'),
                ('c1', 'chat.done'), ('g1', 'result'), ('t2', 'result'), ('t3', 'result')]

        # Two more synthesize requests, a settings read, a streamed chat reply
        # and the ticks all answer while the backend is blocked inside the
        # first render — and no speech event has arrived yet.
        assert _wait(answered), output.frames()
        assert not release.is_set()
        assert not any(item['type'] in ('tts.ready', 'tts.failed')
                       for item in output.frames())
        # The loop idles once the requests are answered, so the heartbeat keeps
        # running during the render instead of waiting for it.
        assert _wait(lambda: len(ticks) >= 2), len(ticks)

        release.set()
        assert _wait(lambda: len([item for item in output.frames()
                                  if item['type'] == 'tts.ready']) == 3), output.frames()

        readies = {}
        for item in output.frames():
            if item['type'] == 'tts.ready':
                assert item['id'] == item['payload']['id']
                readies[item['payload']['id']] = item['payload']
        ids = {item['payload']['request_id'] for item in output.frames()
               if item['type'] == 'result' and item['id'] in ('t1', 't2', 't3')}
        assert set(readies) == ids
        assert len({payload['path'] for payload in readies.values()}) == 2
        assert sorted(payload['cached'] for payload in readies.values()) == \
            [False, False, True]
        assert calls == ['慢', '快']
    finally:
        inp.finish()
        thread.join(timeout=5)
        worker.close()
    assert code.get('value') == 0


def test_serve_wires_the_speech_worker_and_drains_its_events(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(tts, '_run_powershell', _fake_backend(calls))
    _write_settings(tmp_path, deepcopy(DEFAULTS))
    output = _Tee()
    inp = _Gate([_frame('tts.synthesize', {'text': '你好'}, 't1')])
    code = {}
    thread = threading.Thread(
        target=lambda: code.update(value=serve(tmp_path, inp, output, io.BytesIO())),
        daemon=True)
    thread.start()
    try:
        assert _wait(lambda: len([item for item in output.frames()
                                  if item['type'] == 'tts.ready']) == 1), output.frames()
        ready = [item for item in output.frames() if item['type'] == 'tts.ready'][-1]
        results = [item for item in output.frames()
                   if item['id'] == 't1' and item['type'] == 'result']
        assert results and results[0]['payload']['accepted'] is True
        assert results[0]['payload']['request_id'] == ready['payload']['id']
        assert ready['payload']['cached'] is False
        assert ready['payload']['path'].endswith('.wav')
        assert [call['text'] for call in calls] == ['你好']
    finally:
        inp.finish()
        thread.join(timeout=10)
    assert code.get('value') == 0


# Settings: the switch, validation and migrations


def test_voice_is_on_by_default_and_stored_off_values_are_kept(tmp_path):
    store = SettingsStore(tmp_path)
    assert store.settings['voice']['enabled'] is True

    # A settings file that already holds enabled=false keeps it: the default
    # change must not force speech back on for existing users.
    document = deepcopy(DEFAULTS)
    document['voice']['enabled'] = False
    _write_settings(tmp_path, document)
    reopened = SettingsStore(tmp_path)
    assert reopened.settings['voice']['enabled'] is False
    assert reopened.warnings == []


def test_disabled_voice_refuses_without_touching_the_backend(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(tts, '_run_powershell', _fake_backend(calls))
    store = SettingsStore(tmp_path)
    store.update({'voice': {'enabled': False}})

    with pytest.raises(ProtocolError) as caught:
        commands.dispatch(
            {'type': 'tts.synthesize', 'id': 'v2', 'payload': {'text': '你好'}}, store)

    assert caught.value.code == 'voice_disabled'
    assert calls == []


def test_voice_settings_validate_rate_volume_and_type(tmp_path):
    store = SettingsStore(tmp_path)
    assert store.settings['voice'] == {
        'enabled': True, 'voice': '', 'rate': 0, 'volume': 70}
    for patch in ({'voice': {'rate': 11}}, {'voice': {'rate': -11}},
                  {'voice': {'volume': 101}}, {'voice': {'volume': -1}},
                  {'voice': {'voice': 5}}, {'voice': {'enabled': 1}},
                  {'voice': {'unknown': True}}):
        with pytest.raises(ValidationError):
            store.update(patch)


def _write_settings(tmp_path, document):
    (tmp_path / 'settings.json').write_bytes(
        (json.dumps(document, ensure_ascii=False) + '\n').encode('utf-8'))


def test_v5_document_without_voice_gains_defaults(tmp_path):
    # A schema 5 file written before the voice group existed; neither v6 group
    # nor autonomy.default_quiet_minutes was there yet either.
    document = {key: value for key, value in deepcopy(DEFAULTS).items()
                if key not in ('voice', 'behavior', 'performance',
                               'bubble', 'greeting', 'hotkeys')}
    document['schema_version'] = 5
    del document['expression']['tone']
    del document['autonomy']['default_quiet_minutes']
    _write_settings(tmp_path, document)

    store = SettingsStore(tmp_path)

    assert store.settings['voice'] == VOICE_DEFAULTS
    assert json.loads((tmp_path / 'settings.json').read_text('utf-8')) == store.settings
    assert any(f'schema version {SCHEMA_VERSION}' in warning for warning in store.warnings)


def test_v4_document_without_voice_gains_desktop_and_voice_defaults(tmp_path):
    document = deepcopy(DEFAULTS)
    document['schema_version'] = 4
    del document['expression']['tone']
    del document['desktop']
    del document['behavior']
    del document['performance']
    del document['voice']
    del document['bubble']
    del document['greeting']
    del document['hotkeys']
    del document['autonomy']['cloud_enabled']
    del document['autonomy']['default_quiet_minutes']
    _write_settings(tmp_path, document)

    store = SettingsStore(tmp_path)

    assert store.settings['schema_version'] == SCHEMA_VERSION
    assert store.settings['desktop'] == DESKTOP_DEFAULTS
    assert store.settings['voice'] == VOICE_DEFAULTS
