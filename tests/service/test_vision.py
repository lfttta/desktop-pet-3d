"""Vision (3-E) tests: fake capture + fake transport, no network, Linux-safe.

``vision.analyze`` is registered in ``service.commands.HANDLERS`` by the real
import of ``service.vision``, so these tests exercise the same dispatch path the
service uses. Capture is injected everywhere; the real Windows seam is never
called on non-Windows hosts.
"""

import base64
import io
import json
import struct
import sys
import threading
import time
import zlib

import pytest

from service import commands, vision
from service.ai.budget import BudgetLedger, reservation
from service.ai.circuit import CircuitBreaker
from service.ai.provider import TransportError
from service.database import Database
from service.protocol import ProtocolError
from service.runtime import BoundedLog, EventLog, Runtime
from service.settings import SettingsStore
from service.vision import (DEFAULT_PROMPT, IMAGE_TOKEN_RESERVE, SYSTEM_PROMPT,
                            VISION_DEFAULTS, VisionError, VisionService,
                            build_vision_messages, capture_screen, downscale_rgb,
                            encode_png, read_vision_settings, should_capture)

FAKE_KEY = 'test-key-not-real'


class VisionStore(SettingsStore):
    """Real settings store plus an injected vision group (pre-v6 schema)."""

    def __init__(self, directory, group=None):
        super().__init__(directory)
        self._vision_group = group

    @property
    def settings(self):
        value = SettingsStore.settings.fget(self)
        if self._vision_group is not None:
            value['vision'] = dict(self._vision_group)
        return value


class FakeCapture:
    def __init__(self, png=None, delay=0.0, error=None):
        self.png = png if png is not None else encode_png(
            2, 2, bytes([255, 0, 0, 0, 255, 0, 0, 0, 255, 255, 255, 255]))
        self.delay = delay
        self.error = error
        self.calls = []
        self.thread_idents = []

    def __call__(self, monitor=None, max_dimension=1280):
        self.calls.append({'monitor': monitor, 'max_dimension': max_dimension})
        self.thread_idents.append(threading.get_ident())
        if self.delay:
            time.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return {'png': self.png, 'width': 2, 'height': 2}


class FakeResponse:
    def __init__(self, body=b'', headers=None):
        self._body = body
        self.headers = headers or {'Content-Type': 'application/json'}
        self.closed = False

    def read(self, size=-1):
        if size is None or size < 0:
            chunk, self._body = self._body, b''
            return chunk
        chunk, self._body = self._body[:size], self._body[size:]
        return chunk

    def close(self):
        self.closed = True


class ScriptedTransport:
    def __init__(self, scripts):
        self.scripts = list(scripts)
        self.requests = []
        self.calls = 0

    def send(self, url, headers, body, connect_timeout, read_timeout):
        self.requests.append({
            'url': url,
            'headers': dict(headers),
            'body': json.loads(body.decode('utf-8')),
        })
        index = min(self.calls, len(self.scripts) - 1)
        item = self.scripts[index]
        self.calls += 1
        if isinstance(item, Exception):
            raise item
        return item


def json_response(content, prompt=100, completion=20):
    body = json.dumps({
        'model': 'deepseek-chat',
        'choices': [{'message': {'role': 'assistant', 'content': content},
                     'finish_reason': 'stop'}],
        'usage': {'prompt_tokens': prompt, 'completion_tokens': completion,
                  'total_tokens': prompt + completion},
    }, ensure_ascii=False).encode('utf-8')
    return FakeResponse(body=body)


class FakeContext:
    def __init__(self, database=None):
        self.database = database
        self.events = []
        self.hooks = []

    def emit(self, event, payload):
        self.events.append((event, payload))

    def post(self, event, payload):
        self.events.append((event, payload))

    def register_tick(self, hook):
        self.hooks.append(hook)

    def terminal(self):
        for event, payload in reversed(self.events):
            if event in ('vision.done', 'vision.error'):
                return event, payload
        return None


class FakeRuntime:
    def __init__(self):
        self.posted = []
        self.events = EventLog()

    def post(self, request_id, event, payload):
        self.posted.append((request_id, event, payload))


class SpyLedger(BudgetLedger):
    def __init__(self, database):
        super().__init__(database)
        self.status_calls = []
        self.record_calls = []

    def status(self, ai_settings, reserve=None):
        self.status_calls.append((ai_settings, reserve))
        return super().status(ai_settings, reserve)

    def record(self, request_id, model, usage, ai_settings):
        self.record_calls.append((request_id, model, usage, ai_settings))
        return super().record(request_id, model, usage, ai_settings)


_UNSET = object()


def make_env(tmp_path, capture=None, transport=None, group=_UNSET, **kwargs):
    store = VisionStore(tmp_path,
                        group={'enabled': True} if group is _UNSET else group)
    database = Database(tmp_path / 'pet.db')
    store.ai_key_provider = lambda: FAKE_KEY
    if transport is not None:
        store.ai_transport = transport
    service = VisionService(store, database,
                            capture=capture if capture is not None else FakeCapture(),
                            transport=transport, **kwargs)
    store._vision_service = service
    return store, database, service, FakeContext(database)


def pump(service, ctx, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        service.poll()
        terminal = ctx.terminal()
        if terminal is not None:
            return terminal
        time.sleep(0.005)
    raise AssertionError('no terminal vision event: %r' % (ctx.events,))


def analyze(store, ctx, payload=None, request_id='req-1'):
    return commands.dispatch({'type': 'vision.analyze', 'id': request_id,
                              'payload': {} if payload is None else payload},
                             store, ctx)


def rows(database, sql, params=()):
    return database.connection.execute(sql, params).fetchall()


def decode_png(data):
    """Independent strict PNG reader: signature, chunk CRCs, IHDR, filter-0 rows."""
    assert data[:8] == b'\x89PNG\r\n\x1a\n'
    pos = 8
    chunks = []
    while pos < len(data):
        length = struct.unpack('>I', data[pos:pos + 4])[0]
        tag = data[pos + 4:pos + 8]
        payload = data[pos + 8:pos + 8 + length]
        crc = struct.unpack('>I', data[pos + 8 + length:pos + 12 + length])[0]
        assert crc == zlib.crc32(tag + payload) & 0xFFFFFFFF
        chunks.append((tag, payload))
        pos += 12 + length
    assert pos == len(data)
    assert chunks[0][0] == b'IHDR' and chunks[-1] == (b'IEND', b'')
    width, height, depth, color, comp, filt, interlace = struct.unpack(
        '>IIBBBBB', chunks[0][1])
    assert (depth, color, comp, filt, interlace) == (8, 2, 0, 0, 0)
    raw = zlib.decompress(b''.join(payload for tag, payload in chunks
                                   if tag == b'IDAT'))
    stride = width * 3
    assert len(raw) == height * (stride + 1)
    pixels = bytearray()
    for y in range(height):
        row = raw[y * (stride + 1):(y + 1) * (stride + 1)]
        assert row[0] == 0  # filter None
        pixels.extend(row[1:])
    return width, height, bytes(pixels)


# PNG encoder


def test_png_chunks_crc_and_header():
    png = encode_png(3, 2, bytes(range(18)))
    width, height, pixels = decode_png(png)
    assert (width, height) == (3, 2)
    assert pixels == bytes(range(18))


def test_png_pixel_roundtrip_distinct_colors():
    rgb = bytes([255, 0, 0, 0, 255, 0, 0, 0, 255,
                 10, 20, 30, 250, 240, 230, 0, 0, 0])
    width, height, pixels = decode_png(encode_png(3, 2, rgb))
    assert (width, height, pixels) == (3, 2, rgb)


def test_png_single_pixel_matches_framing():
    png = encode_png(1, 1, b'\x01\x02\x03')
    # signature + IHDR(25) + IDAT + IEND(12); IDAT inflates to filter byte + RGB
    assert png[:8] == b'\x89PNG\r\n\x1a\n'
    assert png[12:16] == b'IHDR'
    assert png[-8:-4] == b'IEND'
    assert decode_png(png) == (1, 1, b'\x01\x02\x03')


@pytest.mark.parametrize('width, height, rgb', [
    (0, 1, b''), (1, -1, b'\x00\x00\x00'), (2, 2, b'\x00' * 11),
    (1, 1, None), (1.5, 1, b'\x00\x00\x00'),
])
def test_png_rejects_invalid_input(width, height, rgb):
    with pytest.raises(ValueError):
        encode_png(width, height, rgb)


# Downscale


def test_downscale_keeps_small_images():
    rgb = bytes(range(3 * 4 * 3))
    assert downscale_rgb(4, 3, rgb, 1280) == (4, 3, rgb)
    assert downscale_rgb(4, 3, rgb, 4) == (4, 3, rgb)


def test_downscale_longest_edge_and_aspect():
    rgb = bytes(100 * 50 * 3)
    assert downscale_rgb(100, 50, rgb, 10)[:2] == (10, 5)
    assert downscale_rgb(50, 100, rgb, 10)[:2] == (5, 10)


def test_downscale_nearest_pixels():
    # 8x8 with 2x2 blocks of red/blue; 4x4 nearest samples one pixel per block
    # and must keep the exact block pattern.
    red, blue = b'\xff\x00\x00', b'\x00\x00\xff'
    rgb = b''.join(red if (x // 2 + y // 2) % 2 == 0 else blue
                   for y in range(8) for x in range(8))
    out_w, out_h, scaled = downscale_rgb(8, 8, rgb, 4)
    assert (out_w, out_h) == (4, 4)
    expected = b''.join(red if (x + y) % 2 == 0 else blue
                        for y in range(4) for x in range(4))
    assert scaled == expected


def test_downscale_extreme_aspect_keeps_one_pixel():
    rgb = bytes(1000 * 3 * 3)
    out_w, out_h, scaled = downscale_rgb(1000, 3, rgb, 10)
    assert (out_w, out_h) == (10, 1)
    assert len(scaled) == 30


@pytest.mark.parametrize('maximum', [0, -1, 'x', 1.5, True])
def test_downscale_rejects_invalid_max(maximum):
    with pytest.raises(ValueError):
        downscale_rgb(4, 4, bytes(48), maximum)


# Capture seam


@pytest.mark.skipif(sys.platform == 'win32', reason='non-Windows behavior')
def test_capture_not_available_off_windows():
    with pytest.raises(VisionError) as caught:
        capture_screen()
    assert caught.value.code == 'not_available'
    assert 'Windows' in caught.value.message


@pytest.mark.parametrize('monitor', [-1, 16, True, '0', 1.0])
def test_capture_rejects_bad_monitor(monitor):
    with pytest.raises(VisionError) as caught:
        capture_screen(monitor=monitor)
    assert caught.value.code == 'invalid_request'


# Settings boundary


def test_read_vision_settings_defaults_and_clamps():
    assert read_vision_settings({}) == VISION_DEFAULTS
    assert read_vision_settings({'vision': None}) == VISION_DEFAULTS
    assert read_vision_settings(None) == VISION_DEFAULTS
    good = {'enabled': True, 'model': 'gpt-4o-mini', 'max_dimension': 640,
            'proactive_interval_seconds': 600}
    assert read_vision_settings({'vision': good}) == good
    bad = {'enabled': 'yes', 'model': 5, 'max_dimension': 10,
           'proactive_interval_seconds': 5}
    assert read_vision_settings({'vision': bad}) == VISION_DEFAULTS
    assert read_vision_settings({'vision': {'max_dimension': 7681}})['max_dimension'] == 1280


# Proactive predicate


@pytest.mark.parametrize('now, last, interval, expected', [
    (1000.0, None, 1800, True),
    (1000.0, 900.0, 1800, False),
    (2700.0, 900.0, 1800, True),
    (2701.0, 900.0, 1800, True),
    (800.0, 900.0, 1800, False),      # backwards clock
    (1000.0, 900.0, 0, True),
    (1000.0, 900.0, -5, False),
    (1000.0, 900.0, 'x', False),
    (1000.0, 900.0, True, False),
    ('now', None, 1800, False),
    (1000.0, 'x', 1800, False),
    (float('nan'), None, 1800, False),
    (1000.0, None, float('inf'), False),
])
def test_should_capture(now, last, interval, expected):
    assert should_capture(now, last, interval) is expected


# Command gating


def test_analyze_disabled_is_not_available(tmp_path):
    capture = FakeCapture()
    transport = ScriptedTransport([json_response('不该出现')])
    for index, group in enumerate(({'enabled': False}, None)):
        store, database, service, ctx = make_env(tmp_path / ('case%d' % index),
                                                 capture=capture,
                                                 transport=transport, group=group)
        try:
            with pytest.raises(ProtocolError) as caught:
                analyze(store, ctx)
            assert caught.value.code == 'not_available'
            assert 'vision.enabled' in str(caught.value)
        finally:
            database.close()
    assert capture.calls == []
    assert transport.calls == 0


@pytest.mark.skipif(sys.platform == 'win32', reason='non-Windows behavior')
def test_analyze_enabled_but_not_windows_is_not_available(tmp_path):
    store = VisionStore(tmp_path, group={'enabled': True})
    database = Database(tmp_path / 'pet.db')
    store.ai_key_provider = lambda: FAKE_KEY
    ctx = FakeContext(database)
    try:
        with pytest.raises(ProtocolError) as caught:
            analyze(store, ctx)  # no injected capture: real seam refuses Linux
        assert caught.value.code == 'not_available'
        assert 'Windows' in str(caught.value)
    finally:
        database.close()


@pytest.mark.parametrize('payload', [
    {'prompt': 123}, {'prompt': 'x' * 2001}, {'monitor': -1},
    {'monitor': True}, {'monitor': '0'}, {'monitor': 16}, {'monitor': 1.0},
])
def test_analyze_invalid_params(tmp_path, payload):
    store, database, service, ctx = make_env(tmp_path)
    try:
        with pytest.raises(ProtocolError) as caught:
            analyze(store, ctx, payload)
        assert caught.value.code == 'invalid_request'
        assert service.capture.calls == []
    finally:
        database.close()


# Success flow


def test_analyze_success_default_prompt_and_image(tmp_path):
    capture = FakeCapture()
    transport = ScriptedTransport([json_response('屏幕上开着编辑器。')])
    store, database, service, ctx = make_env(tmp_path, capture=capture,
                                             transport=transport)
    try:
        event, payload = analyze(store, ctx)
        assert event == 'accepted'
        assert payload['request_id'].startswith('vision-')
        terminal, done = pump(service, ctx)
        assert terminal == 'vision.done'
        assert done['text'] == '屏幕上开着编辑器。'
        assert done['usage']['prompt_tokens'] == 100
        assert done['model'] == 'deepseek-chat'
        body = transport.requests[0]['body']
        assert body['model'] == 'deepseek-chat'
        assert body['messages'][0] == {'role': 'system', 'content': SYSTEM_PROMPT}
        parts = body['messages'][1]['content']
        assert parts[0] == {'type': 'text', 'text': DEFAULT_PROMPT}
        url = parts[1]['image_url']['url']
        assert url.startswith('data:image/png;base64,')
        assert base64.b64decode(url.split(',', 1)[1]) == capture.png
        assert transport.requests[0]['headers']['Authorization'] == 'Bearer ' + FAKE_KEY
        assert capture.calls == [{'monitor': None, 'max_dimension': 1280}]
    finally:
        database.close()


def test_analyze_custom_prompt_monitor_and_dimension(tmp_path):
    capture = FakeCapture()
    transport = ScriptedTransport([json_response('好的')])
    store, database, service, ctx = make_env(
        tmp_path, capture=capture, transport=transport,
        group={'enabled': True, 'max_dimension': 640})
    try:
        analyze(store, ctx, {'prompt': '  这在做什么？  ', 'monitor': 1})
        terminal, done = pump(service, ctx)
        assert terminal == 'vision.done'
        assert capture.calls == [{'monitor': 1, 'max_dimension': 640}]
        parts = transport.requests[0]['body']['messages'][1]['content']
        assert parts[0]['text'] == '这在做什么？'
    finally:
        database.close()


def test_vision_model_override_and_fallback(tmp_path):
    transport = ScriptedTransport([json_response('好的')])
    store, database, service, ctx = make_env(
        tmp_path, transport=transport, group={'enabled': True, 'model': 'vision-x'})
    try:
        analyze(store, ctx)
        pump(service, ctx)
        assert transport.requests[0]['body']['model'] == 'vision-x'
        store.update({'ai': {'model': 'chat-y'}})
        store._vision_group = {'enabled': True, 'model': ''}
        ctx.events.clear()
        analyze(store, ctx, request_id='req-2')
        pump(service, ctx)
        assert transport.requests[1]['body']['model'] == 'chat-y'
    finally:
        database.close()


def test_worker_thread_does_not_block_the_caller(tmp_path):
    capture = FakeCapture(delay=0.3)
    transport = ScriptedTransport([json_response('慢慢看')])
    store, database, service, ctx = make_env(tmp_path, capture=capture,
                                             transport=transport)
    try:
        started = time.perf_counter()
        event, payload = analyze(store, ctx)
        elapsed = time.perf_counter() - started
        assert event == 'accepted'
        assert elapsed < 0.05
        assert ctx.terminal() is None
        terminal, done = pump(service, ctx)
        assert terminal == 'vision.done'
        assert done['text'] == '慢慢看'
        assert capture.thread_idents[0] != threading.get_ident()
    finally:
        database.close()


def test_single_flight_rejects_second_request(tmp_path):
    capture = FakeCapture(delay=0.2)
    transport = ScriptedTransport([json_response('第一张'), json_response('第二张')])
    store, database, service, ctx = make_env(tmp_path, capture=capture,
                                             transport=transport)
    try:
        event, _ = analyze(store, ctx)
        assert event == 'accepted'
        with pytest.raises(ProtocolError) as caught:
            analyze(store, ctx, request_id='req-2')
        assert caught.value.code == 'busy'
        pump(service, ctx)
        ctx.events.clear()
        event, _ = analyze(store, ctx, request_id='req-3')
        assert event == 'accepted'
        pump(service, ctx)
        assert transport.calls == 2
    finally:
        database.close()


def test_attach_registers_tick_once(tmp_path):
    store, database, service, ctx = make_env(tmp_path)
    try:
        service.attach(ctx)
        service.attach(ctx)
        # The heartbeat hook must be tick, not poll: poll only drains results,
        # while the proactive capture is decided in tick.
        assert ctx.hooks == [service.tick]
    finally:
        database.close()


def test_bind_registers_once_and_attach_is_noop(tmp_path):
    store, database, service, ctx = make_env(tmp_path)
    hooks = []

    class Binder:
        events = EventLog()

        def register_tick(self, hook):
            hooks.append(hook)

    runtime = Binder()
    try:
        service.bind(runtime)
        service.bind(runtime)
        service.attach(ctx)
        # One heartbeat hook total, and it must be tick: attach() after bind()
        # is a no-op because the proactive capture lives in tick, not poll.
        assert hooks == [service.tick]
        assert ctx.hooks == []
        assert service.runtime is runtime
    finally:
        database.close()


# Budget


def test_budget_reservation_includes_image_tokens(tmp_path):
    transport = ScriptedTransport([json_response('好的')])
    store, database, service, ctx = make_env(tmp_path, transport=transport)
    ledger = SpyLedger(database)
    service.budget = ledger
    try:
        analyze(store, ctx, {'prompt': '看看屏幕'})
        pump(service, ctx)
        ai_settings, reserve = ledger.status_calls[0]
        text_only = reservation([{'role': 'system', 'content': SYSTEM_PROMPT},
                                 {'role': 'user', 'content': '看看屏幕'}], ai_settings)
        assert reserve['tokens'] == text_only['tokens'] + IMAGE_TOKEN_RESERVE
        assert reserve['tokens'] >= IMAGE_TOKEN_RESERVE + ai_settings['max_tokens']
        assert len(ledger.record_calls) == 1
        request_id, model, usage, _ = ledger.record_calls[0]
        assert request_id.startswith('vision-')
        assert model == 'deepseek-chat'
        assert usage['prompt_tokens'] == 100
    finally:
        database.close()


def test_budget_exceeded_blocks_before_capture(tmp_path):
    capture = FakeCapture()
    transport = ScriptedTransport([json_response('不该出现')])
    store, database, service, ctx = make_env(tmp_path, capture=capture,
                                             transport=transport)
    try:
        store.update({'ai': {'daily_token_budget': 10}})
        with database.transaction() as connection:
            connection.execute(
                'INSERT INTO usage_ledger(prompt_tokens, completion_tokens, total_tokens) '
                'VALUES(15, 5, 20)')
        with pytest.raises(ProtocolError) as caught:
            analyze(store, ctx)
        assert caught.value.code == 'budget_exceeded'
        assert 'daily_token_budget' in str(caught.value)
        assert capture.calls == []
        assert transport.calls == 0
        assert rows(database, 'SELECT SUM(total_tokens) FROM usage_ledger') == [(20,)]
    finally:
        database.close()


def test_budget_record_writes_ledger(tmp_path):
    transport = ScriptedTransport([json_response('好的', prompt=1000, completion=500)])
    store, database, service, ctx = make_env(tmp_path, transport=transport)
    try:
        store.update({'ai': {'input_price_per_million': 2.0,
                             'output_price_per_million': 3.0}})
        _, accepted = analyze(store, ctx)
        terminal, done = pump(service, ctx)
        assert terminal == 'vision.done'
        assert done['usage']['recorded'] is True
        assert done['usage']['cost'] == pytest.approx(0.0035)
        ledger = rows(database, 'SELECT request_id, prompt_tokens, completion_tokens, cost '
                                'FROM usage_ledger')
        assert ledger == [(accepted['request_id'], 1000, 500, pytest.approx(0.0035))]
    finally:
        database.close()


# Error mapping


@pytest.mark.parametrize('error, code, retryable', [
    (TransportError('http', 'HTTP 402', status=402,
                    body=b'{"error":{"message":"Insufficient Balance"}}'),
     'payment_required', False),
    (TransportError('http', 'HTTP 429', status=429), 'rate_limited', True),
    (TransportError('timeout', 'timed out'), 'timeout', True),
    (TransportError('connection', 'down'), 'connection_error', True),
])
def test_provider_error_mapping(tmp_path, error, code, retryable):
    transport = ScriptedTransport([error])
    store, database, service, ctx = make_env(tmp_path, transport=transport)
    try:
        analyze(store, ctx)
        terminal, payload = pump(service, ctx)
        assert terminal == 'vision.error'
        assert payload['code'] == code
        assert payload['retryable'] is retryable
        assert payload['message']
        assert transport.calls == 1  # one-shot: no retry loop
    finally:
        database.close()


def test_capture_failure_is_error_event(tmp_path):
    capture = FakeCapture(error=VisionError('capture_failed', 'no screen'))
    transport = ScriptedTransport([json_response('不该出现')])
    store, database, service, ctx = make_env(tmp_path, capture=capture,
                                             transport=transport)
    try:
        analyze(store, ctx)
        terminal, payload = pump(service, ctx)
        assert terminal == 'vision.error'
        assert payload['code'] == 'capture_failed'
        assert payload['retryable'] is False
        assert transport.calls == 0
    finally:
        database.close()


def test_unexpected_capture_exception_maps_capture_failed(tmp_path):
    capture = FakeCapture(error=OSError('win32 exploded'))
    store, database, service, ctx = make_env(tmp_path, capture=capture)
    try:
        analyze(store, ctx)
        terminal, payload = pump(service, ctx)
        assert terminal == 'vision.error'
        assert payload['code'] == 'capture_failed'
        assert 'win32 exploded' not in json.dumps(payload, ensure_ascii=False)
    finally:
        database.close()


def test_missing_key_is_sync_error(tmp_path):
    store = VisionStore(tmp_path, group={'enabled': True})
    database = Database(tmp_path / 'pet.db')
    service = VisionService(store, database, capture=FakeCapture())
    store._vision_service = service
    ctx = FakeContext(database)
    try:
        with pytest.raises(ProtocolError) as caught:
            analyze(store, ctx)
        assert caught.value.code == 'secrets_missing'
        assert service.capture.calls == []
    finally:
        database.close()


def test_circuit_records_failure_and_blocks_next_request(tmp_path):
    now = [1000.0]
    circuit = CircuitBreaker(threshold=1, cooldown_seconds=60.0,
                             clock=lambda: now[0])
    transport = ScriptedTransport([TransportError('timeout', 'timed out')])
    store, database, service, ctx = make_env(tmp_path, transport=transport,
                                             circuit=circuit)
    try:
        analyze(store, ctx)
        terminal, payload = pump(service, ctx)
        assert terminal == 'vision.error'
        assert payload['code'] == 'timeout'
        assert circuit.is_open() is True
        with pytest.raises(ProtocolError) as caught:
            analyze(store, ctx, request_id='req-2')
        assert caught.value.code == 'circuit_open'
        assert transport.calls == 1
        now[0] += 61.0
        transport.scripts.append(json_response('恢复了'))
        ctx.events.clear()
        analyze(store, ctx, request_id='req-3')
        terminal, done = pump(service, ctx)
        assert terminal == 'vision.done'
        assert done['text'] == '恢复了'
        assert circuit.is_open() is False
    finally:
        database.close()


# Proactive entry point


def test_maybe_capture_gating_interval_and_delivery(tmp_path):
    clock = [1000.0]
    capture = FakeCapture()
    transport = ScriptedTransport([json_response('你在写代码。'),
                                   json_response('你在看视频。')])
    store, database, service, ctx = make_env(tmp_path, capture=capture,
                                             transport=transport,
                                             clock=lambda: clock[0])
    runtime = FakeRuntime()
    service.runtime = runtime
    try:
        assert service.maybe_capture() is not None
        assert len(capture.calls) == 1
        assert service.maybe_capture() is None  # interval not elapsed
        deadline = time.monotonic() + 3.0
        while not runtime.posted and time.monotonic() < deadline:
            service.poll()
            time.sleep(0.005)
        assert runtime.posted[0][0] == 'vision'
        assert runtime.posted[0][1] == 'vision.proactive'
        assert runtime.posted[0][2]['text'] == '你在写代码。'
        clock[0] += 1800.0
        assert service.maybe_capture() is not None
        assert len(capture.calls) == 2
        deadline = time.monotonic() + 3.0
        while len(runtime.posted) < 2 and time.monotonic() < deadline:
            service.poll()
            time.sleep(0.005)
        assert runtime.posted[1][2]['text'] == '你在看视频。'
    finally:
        database.close()


def test_maybe_capture_disabled_or_unavailable_never_captures(tmp_path):
    capture = FakeCapture()
    store, database, service, ctx = make_env(tmp_path, capture=capture,
                                             group={'enabled': False})
    try:
        assert service.maybe_capture(now=1000.0) is None
        assert capture.calls == []
    finally:
        database.close()


def test_maybe_capture_failure_does_not_retry_every_tick(tmp_path):
    capture = FakeCapture(error=VisionError('capture_failed', 'no screen'))
    store, database, service, ctx = make_env(tmp_path, capture=capture)
    runtime = FakeRuntime()
    service.runtime = runtime
    try:
        assert service.maybe_capture(now=1000.0) is not None
        deadline = time.monotonic() + 3.0
        while service.pending_count() and time.monotonic() < deadline:
            time.sleep(0.005)
        service.poll()
        assert service.maybe_capture(now=1001.0) is None
        assert len(capture.calls) == 1
        assert runtime.posted == []  # proactive failures never emit frames
        assert any(entry['code'] == 'error' for entry in runtime.events.tail())
    finally:
        database.close()


# Privacy


def test_no_image_or_key_in_frames_logs_or_disk(tmp_path):
    capture = FakeCapture()
    transport = ScriptedTransport([json_response('屏幕上有个终端。')])
    store, database, service, ctx = make_env(tmp_path, capture=capture,
                                             transport=transport)
    runtime = FakeRuntime()
    service.runtime = runtime
    try:
        analyze(store, ctx)
        terminal, done = pump(service, ctx)
        assert terminal == 'vision.done'
        serialized = json.dumps(ctx.events, ensure_ascii=False)
        assert 'base64' not in serialized
        assert base64.b64encode(capture.png).decode('ascii')[:32] not in serialized
        assert FAKE_KEY not in serialized
        assert 'image' not in done and 'png' not in done
        for entry in runtime.events.tail():
            assert FAKE_KEY not in json.dumps(entry)
            assert 'base64' not in json.dumps(entry)
        assert list(tmp_path.glob('*.png')) == []
        assert FAKE_KEY not in (tmp_path / 'settings.json').read_text('utf-8')
    finally:
        database.close()


# Runtime integration


class FrameThenIdleInput:
    def __init__(self, frame, idle=0.4):
        self.frame = frame
        self.idle = idle
        self.sent = False

    def read1(self, size):
        if not self.sent:
            self.sent = True
            return self.frame
        time.sleep(self.idle)
        return b''

    def read(self, size):
        return self.read1(size)


def test_runtime_tick_drains_vision_frames(tmp_path):
    capture = FakeCapture()
    transport = ScriptedTransport([json_response('运行中的识屏')])
    store, database, service, ctx = make_env(tmp_path, capture=capture,
                                             transport=transport)
    frame = json.dumps({'version': 1, 'id': 'req-1', 'type': 'vision.analyze',
                        'payload': {}}, ensure_ascii=False).encode('utf-8') + b'\n'
    output = io.BytesIO()
    runtime = Runtime(store, output, BoundedLog(io.BytesIO()), database,
                      tick_seconds=0.01)
    service.bind(runtime)  # the same wiring service.runtime.serve() performs
    try:
        assert runtime.run(FrameThenIdleInput(frame)) == 0
    finally:
        database.close()
    replies = [json.loads(line) for line in
               output.getvalue().decode('utf-8').splitlines()]
    types = [reply['type'] for reply in replies]
    assert types[0] == 'accepted'
    assert types[-1] == 'vision.done'
    assert all(reply['id'] == 'req-1' for reply in replies)
    assert replies[-1]['payload']['text'] == '运行中的识屏'


def test_build_vision_messages_shape():
    png = encode_png(1, 1, b'\x00\x00\x00')
    messages = build_vision_messages(png, '自定义问题')
    assert messages[0]['role'] == 'system'
    assert '桌面宠物' in messages[0]['content']
    parts = messages[1]['content']
    assert parts[0] == {'type': 'text', 'text': '自定义问题'}
    assert parts[1]['type'] == 'image_url'
    assert parts[1]['image_url']['url'].startswith('data:image/png;base64,')


def test_serve_registers_and_closes_the_vision_tick(tmp_path, monkeypatch):
    """The wiring itself: serve() must register vision.tick and close it."""
    from service import runtime as runtime_module
    from service import vision as vision_module

    created = []

    class FakeVision:
        def __init__(self, runtime):
            self.runtime = runtime
            self.closed = 0
            self.bound = 0
            created.append(self)

        def bind(self, runtime):
            self.bound += 1
            runtime.register_tick(self.tick)

        def tick(self, now=None):
            pass

        def close(self):
            self.closed += 1

    monkeypatch.setattr(vision_module, 'get_vision_service',
                        lambda store, database=None, runtime=None: FakeVision(runtime))
    output, errors = io.BytesIO(), io.BytesIO()
    assert runtime_module.serve(tmp_path, io.BytesIO(b''), output, errors) == 0
    assert len(created) == 1
    assert created[0].bound == 1
    assert created[0].closed == 1
    assert created[0].tick in created[0].runtime._tick_hooks


def test_vision_tick_throttles_the_proactive_check(tmp_path):
    """A 100 ms tick must not re-read settings on every call."""
    clock = [1000.0]
    capture = FakeCapture()
    store, database, service, ctx = make_env(tmp_path, capture=capture,
                                             clock=lambda: clock[0])

    try:
        for _ in range(5):
            service.tick()
        # The first tick captures; the rest fall inside the 1 s check window.
        assert len(capture.calls) == 1
        clock[0] += 1.0
        service.tick()
        # Interval (1800 s) still not elapsed, so no second capture.
        assert len(capture.calls) == 1
    finally:
        database.close()
