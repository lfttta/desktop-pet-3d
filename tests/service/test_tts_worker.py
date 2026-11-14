"""Speech queue lifecycle and the voice command's refusals.

The worker is the only place the service still blocks on a subprocess, so its
lifecycle matters: a closed queue must refuse new work, a full queue must refuse
instead of blocking the service thread, a dead render thread must mark itself
closed, and a crash inside one render must not escape.
"""

import io
import queue
import threading

import pytest

from service.features import voice
from service.protocol import ProtocolError
from service.runtime import Runtime, TtsWorker


class _Recorder:
    """Stands in for a Runtime: records posted frames, no transport."""

    def __init__(self):
        self.frames = []
        self.speech = None

    def post(self, request_id, event, payload):
        self.frames.append((request_id, event, payload))


@pytest.fixture()
def runtime():
    return _Recorder()


def _job(text='你好', voice_name='', rate=0, volume=70, request_id='tts-1'):
    return {'id': request_id, 'text': text, 'voice': voice_name,
            'rate': rate, 'volume': volume}


def _worker(runtime, tmp_path, maxsize=2):
    return TtsWorker(runtime, tmp_path, maxsize=maxsize)


# -- queue lifecycle -------------------------------------------------------


def test_a_closed_worker_refuses_submit(runtime, tmp_path):
    worker = _worker(runtime, tmp_path)
    worker.close()

    with pytest.raises(RuntimeError, match='closed'):
        worker.submit(_job())


def test_a_closed_worker_closes_idempotently(runtime, tmp_path):
    worker = _worker(runtime, tmp_path)
    worker.close()
    worker.close()  # must not raise or hang

    assert worker._closed is True


def test_a_full_queue_refuses_instead_of_blocking(runtime, tmp_path):
    worker = _worker(runtime, tmp_path, maxsize=1)
    worker.submit(_job())

    with pytest.raises(queue.Full):
        worker.submit(_job(request_id='tts-2'))


def test_a_start_after_close_reports_failure(runtime, tmp_path):
    worker = _worker(runtime, tmp_path)
    worker.close()

    assert worker.start() is False


def test_a_worker_whose_thread_cannot_start_marks_itself_closed(runtime, tmp_path, monkeypatch):
    worker = _worker(runtime, tmp_path)

    def explode():
        raise RuntimeError('cannot start new thread')

    monkeypatch.setattr(worker._worker, 'start', explode)

    assert worker.start() is False
    assert worker._closed is True
    with pytest.raises(RuntimeError):
        worker.submit(_job())


# -- rendering outcomes ----------------------------------------------------


def test_a_successful_render_posts_ready(runtime, tmp_path, monkeypatch):
    worker = _worker(runtime, tmp_path)
    monkeypatch.setattr(
        'service.runtime.tts.synthesize',
        lambda *args, **kwargs: {'path': 'x.wav', 'voice': 'v', 'cached': False})

    worker._render(_job(request_id='tts-9'))

    assert runtime.frames == [('tts-9', 'tts.ready',
                               {'id': 'tts-9', 'path': 'x.wav',
                                'voice': 'v', 'cached': False})]


def test_a_protocol_error_from_the_backend_posts_failed(runtime, tmp_path, monkeypatch):
    worker = _worker(runtime, tmp_path)

    def explode(*_args, **_kwargs):
        raise ProtocolError('tts_unavailable', 'Speech synthesis is unavailable.')

    monkeypatch.setattr('service.runtime.tts.synthesize', explode)

    worker._render(_job(request_id='tts-9'))

    request_id, event, payload = runtime.frames[0]
    assert (request_id, event) == ('tts-9', 'tts.failed')
    assert 'unavailable' in payload['message']


def test_an_unexpected_exception_posts_a_generic_failure(runtime, tmp_path, monkeypatch):
    worker = _worker(runtime, tmp_path)

    def explode(*_args, **_kwargs):
        raise ValueError('bad state')

    monkeypatch.setattr('service.runtime.tts.synthesize', explode)

    worker._render(_job(request_id='tts-9'))

    _request_id, event, payload = runtime.frames[0]
    assert event == 'tts.failed'
    assert payload['message'] == 'Speech synthesis is unavailable.'


def test_a_failing_post_does_not_escape_the_render(runtime, tmp_path, monkeypatch):
    worker = _worker(runtime, tmp_path)

    def explode(*_args, **_kwargs):
        raise RuntimeError('runtime is gone')

    monkeypatch.setattr(runtime, 'post', explode)
    monkeypatch.setattr(
        'service.runtime.tts.synthesize',
        lambda *args, **kwargs: {'path': 'x.wav', 'voice': '', 'cached': False})

    worker._render(_job())  # must not raise


def test_one_failed_render_does_not_stop_the_thread(runtime, tmp_path, monkeypatch):
    worker = _worker(runtime, tmp_path)
    calls = []

    def flaky(text, *args, **kwargs):
        calls.append(text)
        if len(calls) == 1:
            raise ValueError('first render explodes')
        return {'path': 'ok.wav', 'voice': '', 'cached': False}

    monkeypatch.setattr('service.runtime.tts.synthesize', flaky)

    worker._render(_job(text='第一条', request_id='tts-1'))
    worker._render(_job(text='第二条', request_id='tts-2'))

    events = [event for _id, event, _payload in runtime.frames]
    assert events == ['tts.failed', 'tts.ready']


# -- voice command ---------------------------------------------------------


class _Store:
    def __init__(self, directory, settings):
        self.path = directory / 'settings.json'
        self._settings = settings

    @property
    def settings(self):
        return self._settings


def _context(runtime, speech=None):
    class _Ctx:
        pass

    context = _Ctx()
    context.speech = speech
    context.runtime = runtime
    return context


def _req(payload, request_id='r1'):
    return {'type': 'tts.synthesize', 'id': request_id, 'payload': payload}


def test_a_non_string_voice_is_rejected(tmp_path, monkeypatch):
    store = _Store(tmp_path, {'voice': {'enabled': True}})

    with pytest.raises(ProtocolError) as caught:
        voice.synthesize(_req({'text': '你好', 'voice': 7}), store,
                         _context(_Recorder()))

    assert caught.value.code == 'invalid_request'


def test_a_store_without_a_directory_reports_storage_unavailable(monkeypatch):
    class _NoPath:
        @property
        def settings(self):
            return {'voice': {'enabled': True}}

    with pytest.raises(ProtocolError) as caught:
        voice.synthesize(_req({'text': '你好'}), _NoPath(), _context(_Recorder()))

    assert caught.value.code == 'storage_unavailable'


def test_a_prepare_failure_keeps_its_code(tmp_path, monkeypatch):
    store = _Store(tmp_path, {'voice': {'enabled': True}})

    def explode(*_args, **_kwargs):
        raise ProtocolError('tts_unavailable', 'nope')

    monkeypatch.setattr('service.features.voice.tts.prepare', explode)

    with pytest.raises(ProtocolError) as caught:
        voice.synthesize(_req({'text': '你好'}), store, _context(_Recorder()))

    assert caught.value.code == 'tts_unavailable'


def test_a_full_speech_queue_reports_busy(tmp_path, monkeypatch):
    store = _Store(tmp_path, {'voice': {'enabled': True}})
    speech = TtsWorker(_Recorder(), tmp_path, maxsize=1)
    speech.submit(_job())
    monkeypatch.setattr(
        'service.features.voice.tts.prepare',
        lambda text, **kwargs: {'text': text, 'voice': '', 'rate': 0, 'volume': 70})

    with pytest.raises(ProtocolError) as caught:
        voice.synthesize(_req({'text': '你好'}), store, _context(_Recorder(), speech))

    assert caught.value.code == 'tts_busy', (
        'a full queue is a temporary condition, not a broken backend')


def test_a_closed_speech_queue_reports_unavailable(tmp_path, monkeypatch):
    store = _Store(tmp_path, {'voice': {'enabled': True}})
    speech = TtsWorker(_Recorder(), tmp_path)
    speech.close()
    monkeypatch.setattr(
        'service.features.voice.tts.prepare',
        lambda text, **kwargs: {'text': text, 'voice': '', 'rate': 0, 'volume': 70})

    with pytest.raises(ProtocolError) as caught:
        voice.synthesize(_req({'text': '你好'}), store, _context(_Recorder(), speech))

    assert caught.value.code == 'tts_unavailable'


def test_an_accepted_request_answers_with_a_synthesis_id(tmp_path, monkeypatch):
    store = _Store(tmp_path, {'voice': {'enabled': True}})
    recorder = _Recorder()
    speech = TtsWorker(recorder, tmp_path)
    submitted = []
    monkeypatch.setattr(
        'service.features.voice.tts.prepare',
        lambda text, **kwargs: {'text': text, 'voice': '', 'rate': 0, 'volume': 70})
    monkeypatch.setattr(speech, 'submit', lambda job: submitted.append(job))

    event, payload = voice.synthesize(_req({'text': '  你好  '}), store,
                                      _context(recorder, speech))

    assert event == 'result', 'the command answers with a normal result frame'
    assert payload['accepted'] is True
    assert payload['request_id'].startswith(voice.ID_PREFIX)
    # prepare() is the layer that trims; it is patched out here, so the job
    # carries the text verbatim. Trimming is covered in test_tts.py.
    assert submitted and submitted[0]['text'] == '  你好  '
    assert submitted[0]['id'] == payload['request_id']


def test_capabilities_advertise_the_command():
    assert voice.CAPABILITY['available'] is True
    assert voice.CAPABILITY['commands'] == ['tts.synthesize']
