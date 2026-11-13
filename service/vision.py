"""Screenshot vision (roadmap 3-E): capture, stdlib PNG encoding, one-shot analysis.

Windows-only screen capture through ctypes (user32/gdi32); the PNG encoder and
the nearest-neighbour downscaler are pure stdlib (zlib + struct). The captured
image lives in memory only: it is sent once to the provider as a base64 data
URL and then discarded. Nothing here writes images to disk, and neither image
data nor API keys ever reach a log or an event payload.

Threading mirrors the chat orchestrator and the proactive scheduler: the
service thread validates, gates on ``vision.enabled``, reserves budget, reads
the key and starts one worker; the worker performs capture plus the single
provider call; ``poll`` drains finished work on the service thread, records
usage in the ledger and emits ``vision.done`` / ``vision.error`` frames.

Proactive hook (not wired into the scheduler here): ``should_capture`` is a
pure interval predicate and ``VisionService.maybe_capture`` is the
capture-and-describe entry point a scheduler tick may call. It only acts when
``vision.enabled`` is true and the configured interval has elapsed.

Provider reuse: HTTP, SSE/JSON reading and error normalization come from
``service.ai.provider``; budget reservation/recording from ``service.ai.budget``;
the cooldown circuit from ``service.ai.circuit``. The only wrap needed is a
per-request model override (``provider.model``), because the provider has no
per-call model parameter.
"""

import base64
import ctypes
import math
import queue
import struct
import sys
import threading
import time
import uuid
import zlib

from service.ai.budget import BudgetLedger, reservation
from service.ai.circuit import CircuitBreaker
from service.ai.provider import OpenAICompatibleProvider, ProviderError
from service.protocol import ProtocolError
from service.secrets import SecretsError, load_key_for
from service.settings import (VISION_DEFAULTS, VISION_MAX_DIMENSION_RANGE,
                              VISION_MAX_MODEL_CHARS,
                              VISION_PROACTIVE_INTERVAL_RANGE)

NAME = 'vision'
CAPABILITY = {
    'available': sys.platform == 'win32',
    'reason': '' if sys.platform == 'win32' else 'windows_only',
    'commands': ['vision.analyze'],
}

DEFAULT_PROMPT = '描述屏幕上正在发生什么，用一两句话'
SYSTEM_PROMPT = (
    '你是一只 3D 桌面宠物的视觉助手，负责观察用户的电脑屏幕截图。'
    '请用中文简短、如实地回答用户的问题；只描述截图中真实可见的内容，'
    '不要编造，不要输出与问题无关的内容，默认用一两句话。'
)
MAX_PROMPT_CHARS = 2000
MAX_MONITOR_INDEX = 15

# The vision settings group and its bounds live in service/settings.py, which stays
# free of the AI stack; the import above pulls them in so there is one
# definition. These aliases keep this module's public constant names stable.
DEFAULT_MAX_DIMENSION = VISION_DEFAULTS['max_dimension']
MIN_MAX_DIMENSION, MAX_MAX_DIMENSION = VISION_MAX_DIMENSION_RANGE
DEFAULT_PROACTIVE_INTERVAL = VISION_DEFAULTS['proactive_interval_seconds']
MIN_PROACTIVE_INTERVAL, MAX_PROACTIVE_INTERVAL = VISION_PROACTIVE_INTERVAL_RANGE
MAX_MODEL_CHARS = VISION_MAX_MODEL_CHARS

# Conservative worst-case image token estimate for a <=1280px screenshot;
# added to the text reservation before the ledger is consulted.
IMAGE_TOKEN_RESERVE = 1100
# Guard against absurd virtual-screen sizes (e.g. 8K x 8K walls).
MAX_CAPTURE_PIXELS = 64_000_000

PROACTIVE_REQUEST_ID = 'vision'
PROACTIVE_EVENT = 'vision.proactive'
JOIN_TIMEOUT_SECONDS = 1.0
# Minimum wall-clock spacing between proactive checks made by ``tick``. The
# configured interval is at least MIN_PROACTIVE_INTERVAL (60s), so a 1 Hz check
# is amply precise while avoiding a settings deepcopy every 100 ms tick.
PROACTIVE_CHECK_SECONDS = 1.0

# Chinese event messages for vision-specific codes; provider codes reuse the
# orchestrator's SAFE_ERRORS vocabulary (imported lazily so that settings.py
# can import this module without pulling in the whole AI stack).
VISION_MESSAGES = {
    'not_available': '截屏视觉现在不可用。',
    'budget_exceeded': 'AI 额度用完了，暂时不能识屏。',
    'capture_failed': '这次没能截取到屏幕，稍后再试好吗？',
    'busy': '我还在看上一张截图，稍等一下。',
}


class VisionError(Exception):
    """Vision failure with a protocol-friendly code; never carries image data."""

    def __init__(self, code, message, retryable=False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = bool(retryable)


def _message(code):
    if code in VISION_MESSAGES:
        return VISION_MESSAGES[code]
    from service.ai.orchestrator import SAFE_ERRORS, SAFE_FALLBACK
    return SAFE_ERRORS.get(str(code), SAFE_FALLBACK)


def read_vision_settings(settings):
    """Normalize the vision group at the module boundary.

    Missing, mistyped or out-of-range values fall back to the defaults, so a
    settings document from before the vision group existed simply reads as disabled.
    """
    group = settings.get('vision') if isinstance(settings, dict) else None
    group = group if isinstance(group, dict) else {}
    result = dict(VISION_DEFAULTS)
    enabled = group.get('enabled')
    if type(enabled) is bool:
        result['enabled'] = enabled
    model = group.get('model')
    if type(model) is str and len(model) <= MAX_MODEL_CHARS:
        result['model'] = model
    dimension = group.get('max_dimension')
    if type(dimension) is int and MIN_MAX_DIMENSION <= dimension <= MAX_MAX_DIMENSION:
        result['max_dimension'] = dimension
    interval = group.get('proactive_interval_seconds')
    if type(interval) is int and MIN_PROACTIVE_INTERVAL <= interval <= MAX_PROACTIVE_INTERVAL:
        result['proactive_interval_seconds'] = interval
    return result


# --------------------------------------------------------------------------
# Minimal PNG encoder (RGB8, filter 0) — stdlib only.
# --------------------------------------------------------------------------

PNG_SIGNATURE = b'\x89PNG\r\n\x1a\n'


def _png_chunk(tag, data):
    return (struct.pack('>I', len(data)) + tag + data
            + struct.pack('>I', zlib.crc32(tag + data) & 0xFFFFFFFF))


def encode_png(width, height, rgb):
    """Encode tightly packed RGB8 bytes as a PNG; raises ValueError on bad input."""
    if type(width) is not int or type(height) is not int or width <= 0 or height <= 0:
        raise ValueError('PNG dimensions must be positive integers.')
    if not isinstance(rgb, (bytes, bytearray)) or len(rgb) != width * height * 3:
        raise ValueError('PNG pixel buffer must hold width*height*3 bytes.')
    stride = width * 3
    raw = bytearray()
    for y in range(height):
        raw.append(0)  # filter type 0 (None) per scanline
        raw.extend(rgb[y * stride:(y + 1) * stride])
    header = struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)
    return (PNG_SIGNATURE + _png_chunk(b'IHDR', header)
            + _png_chunk(b'IDAT', zlib.compress(bytes(raw), 6))
            + _png_chunk(b'IEND', b''))


def downscale_rgb(width, height, rgb, max_dimension):
    """Nearest-neighbour downscale so the longest edge is <= max_dimension.

    Returns (width, height, rgb) unchanged when no scaling is needed; never
    upscales. Pure Python but slice-based, fast enough for a one-shot capture.
    """
    if type(width) is not int or type(height) is not int or width <= 0 or height <= 0:
        raise ValueError('image dimensions must be positive integers.')
    if not isinstance(rgb, (bytes, bytearray)) or len(rgb) != width * height * 3:
        raise ValueError('pixel buffer must hold width*height*3 bytes.')
    if type(max_dimension) is not int or max_dimension < 1:
        raise ValueError('max_dimension must be a positive integer.')
    longest = max(width, height)
    if longest <= max_dimension:
        return width, height, bytes(rgb)
    out_w = max(1, width * max_dimension // longest)
    out_h = max(1, height * max_dimension // longest)
    stride = width * 3
    offsets = [3 * (x * width // out_w) for x in range(out_w)]
    rows = []
    for y in range(out_h):
        base = (y * height // out_h) * stride
        rows.append(b''.join(rgb[base + off:base + off + 3] for off in offsets))
    return out_w, out_h, b''.join(rows)


# --------------------------------------------------------------------------
# Windows screen capture (ctypes). Import-safe on every platform.
# --------------------------------------------------------------------------

SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79
SRCCOPY = 0x00CC0020
CAPTUREBLT = 0x40000000
BI_RGB = 0
DIB_RGB_COLORS = 0


class _Rect(ctypes.Structure):
    _fields_ = (('left', ctypes.c_long), ('top', ctypes.c_long),
                ('right', ctypes.c_long), ('bottom', ctypes.c_long))


class _MonitorInfo(ctypes.Structure):
    _fields_ = (('cbSize', ctypes.c_ulong), ('rcMonitor', _Rect),
                ('rcWork', _Rect), ('dwFlags', ctypes.c_ulong))


class _BitmapInfoHeader(ctypes.Structure):
    _fields_ = (('biSize', ctypes.c_uint32), ('biWidth', ctypes.c_int32),
                ('biHeight', ctypes.c_int32), ('biPlanes', ctypes.c_uint16),
                ('biBitCount', ctypes.c_uint16), ('biCompression', ctypes.c_uint32),
                ('biSizeImage', ctypes.c_uint32), ('biXPelsPerMeter', ctypes.c_int32),
                ('biYPelsPerMeter', ctypes.c_int32), ('biClrUsed', ctypes.c_uint32),
                ('biClrImportant', ctypes.c_uint32))


def capture_screen(monitor=None, max_dimension=DEFAULT_MAX_DIMENSION):
    """Capture the virtual screen (monitor=None) or one monitor index.

    Returns {'png': bytes, 'width': int, 'height': int}. Raises VisionError:
    'invalid_request' for bad arguments, 'not_available' off Windows,
    'capture_failed' when a Win32 call fails. This function is the injectable
    capture seam; tests replace it with a fake.
    """
    if monitor is not None and (type(monitor) is not int
                                or not 0 <= monitor <= MAX_MONITOR_INDEX):
        raise VisionError('invalid_request',
                          'monitor must be an integer from 0 to %d.' % MAX_MONITOR_INDEX)
    if type(max_dimension) is not int or not 1 <= max_dimension <= MAX_MAX_DIMENSION:
        raise VisionError('invalid_request', 'max_dimension is out of range.')
    if sys.platform != 'win32':
        raise VisionError('not_available',
                          'Screen capture is only available on Windows.')
    try:
        return _capture_windows(monitor, max_dimension)
    except VisionError:
        raise
    except Exception:
        raise VisionError('capture_failed', 'Screen capture failed.') from None


def _capture_windows(monitor, max_dimension):
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    gdi32 = ctypes.WinDLL('gdi32', use_last_error=True)
    user32.GetSystemMetrics.restype = ctypes.c_int
    user32.GetSystemMetrics.argtypes = (ctypes.c_int,)
    user32.GetDC.restype = ctypes.c_void_p
    user32.GetDC.argtypes = (ctypes.c_void_p,)
    user32.ReleaseDC.restype = ctypes.c_int
    user32.ReleaseDC.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
    user32.GetMonitorInfoW.restype = ctypes.c_int
    user32.GetMonitorInfoW.argtypes = (ctypes.c_void_p, ctypes.POINTER(_MonitorInfo))
    gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
    gdi32.CreateCompatibleDC.argtypes = (ctypes.c_void_p,)
    gdi32.CreateCompatibleBitmap.restype = ctypes.c_void_p
    gdi32.CreateCompatibleBitmap.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_int)
    gdi32.SelectObject.restype = ctypes.c_void_p
    gdi32.SelectObject.argtypes = (ctypes.c_void_p, ctypes.c_void_p,)
    gdi32.BitBlt.restype = ctypes.c_bool
    gdi32.BitBlt.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                             ctypes.c_int, ctypes.c_int, ctypes.c_void_p,
                             ctypes.c_int, ctypes.c_int, ctypes.c_uint32)
    gdi32.GetDIBits.restype = ctypes.c_int
    gdi32.GetDIBits.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint,
                                ctypes.c_uint, ctypes.c_void_p,
                                ctypes.POINTER(_BitmapInfoHeader), ctypes.c_uint)
    gdi32.DeleteObject.restype = ctypes.c_bool
    gdi32.DeleteObject.argtypes = (ctypes.c_void_p,)
    gdi32.DeleteDC.restype = ctypes.c_bool
    gdi32.DeleteDC.argtypes = (ctypes.c_void_p,)

    if monitor is None:
        left = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
        top = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
        width = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
        height = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
    else:
        rects = _monitor_rects(user32)
        if monitor >= len(rects):
            raise VisionError('invalid_request', 'monitor index is out of range.')
        left, top, right, bottom = rects[monitor]
        width, height = right - left, bottom - top
    if width <= 0 or height <= 0:
        raise VisionError('capture_failed', 'The screen rectangle is empty.')
    if width * height > MAX_CAPTURE_PIXELS:
        raise VisionError('capture_failed', 'The virtual screen is too large to capture.')

    screen_dc = user32.GetDC(None)
    if not screen_dc:
        raise VisionError('capture_failed', 'The screen device context is unavailable.')
    mem_dc = None
    bitmap = None
    previous = None
    try:
        mem_dc = gdi32.CreateCompatibleDC(screen_dc)
        if not mem_dc:
            raise VisionError('capture_failed', 'A compatible device context could not be created.')
        bitmap = gdi32.CreateCompatibleBitmap(screen_dc, width, height)
        if not bitmap:
            raise VisionError('capture_failed', 'A capture bitmap could not be created.')
        previous = gdi32.SelectObject(mem_dc, bitmap)
        if not gdi32.BitBlt(mem_dc, 0, 0, width, height, screen_dc, left, top,
                            SRCCOPY | CAPTUREBLT):
            raise VisionError('capture_failed', 'The screen could not be copied.')
        header = _BitmapInfoHeader()
        header.biSize = ctypes.sizeof(_BitmapInfoHeader)
        header.biWidth = width
        header.biHeight = -height  # top-down rows
        header.biPlanes = 1
        header.biBitCount = 32
        header.biCompression = BI_RGB
        buffer = (ctypes.c_ubyte * (width * height * 4))()
        lines = gdi32.GetDIBits(mem_dc, bitmap, 0, height, buffer,
                                ctypes.byref(header), DIB_RGB_COLORS)
        if lines != height:
            raise VisionError('capture_failed', 'The capture bitmap could not be read.')
        raw = bytes(buffer)
    finally:
        if mem_dc and previous:
            gdi32.SelectObject(mem_dc, previous)
        if bitmap:
            gdi32.DeleteObject(bitmap)
        if mem_dc:
            gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(None, screen_dc)

    # BGRA -> RGB without a per-pixel Python loop.
    data = bytearray(raw)
    del data[3::4]
    rgb = bytearray(data)
    rgb[0::3] = data[2::3]
    rgb[2::3] = data[0::3]
    out_w, out_h, scaled = downscale_rgb(width, height, bytes(rgb), max_dimension)
    return {'png': encode_png(out_w, out_h, scaled), 'width': out_w, 'height': out_h}


def _monitor_rects(user32):
    """Monitor rectangles ordered left-to-right, top-to-bottom for stable indices."""
    rects = []
    proc_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p,
                                   ctypes.POINTER(_Rect), ctypes.c_void_p)

    def collect(hmonitor, hdc, lprect, lparam):
        info = _MonitorInfo()
        info.cbSize = ctypes.sizeof(_MonitorInfo)
        if user32.GetMonitorInfoW(hmonitor, ctypes.byref(info)):
            area = info.rcMonitor
            rects.append((area.left, area.top, area.right, area.bottom))
        return True

    user32.EnumDisplayMonitors.restype = ctypes.c_bool
    user32.EnumDisplayMonitors.argtypes = (ctypes.c_void_p, ctypes.c_void_p,
                                           proc_type, ctypes.c_void_p)
    callback = proc_type(collect)
    if not user32.EnumDisplayMonitors(None, None, callback, 0):
        raise VisionError('capture_failed', 'Monitors could not be enumerated.')
    rects.sort(key=lambda rect: (rect[0], rect[1]))
    if not rects:
        raise VisionError('capture_failed', 'No monitor was found.')
    return rects


# --------------------------------------------------------------------------
# Vision prompt assembly.
# --------------------------------------------------------------------------

def image_data_url(png_bytes):
    return 'data:image/png;base64,' + base64.b64encode(png_bytes).decode('ascii')


def build_vision_messages(png_bytes, prompt=DEFAULT_PROMPT):
    """One OpenAI-compatible chat request carrying the screenshot as a data URL."""
    return [
        {'role': 'system', 'content': SYSTEM_PROMPT},
        {'role': 'user', 'content': [
            {'type': 'text', 'text': prompt},
            {'type': 'image_url', 'image_url': {'url': image_data_url(png_bytes)}},
        ]},
    ]


# --------------------------------------------------------------------------
# Proactive hook (pure predicate + entry point; the scheduler owns the wiring).
# --------------------------------------------------------------------------

def _finite_number(value):
    return (type(value) in (int, float) and math.isfinite(value))


def should_capture(now, last_capture, interval):
    """True when a proactive capture is due; conservative on unusable input.

    ``now`` and ``last_capture`` are epoch seconds; ``last_capture=None`` means
    never captured. A negative/non-finite interval, a non-numeric clock or a
    backwards clock never triggers a capture.
    """
    if not _finite_number(interval) or interval < 0:
        return False
    if not _finite_number(now):
        return False
    if last_capture is None:
        return True
    if not _finite_number(last_capture):
        return False
    return now >= last_capture and now - last_capture >= interval


class VisionService:
    """One capture-and-analyze request at a time, owned by the service thread."""

    def __init__(self, store, database=None, runtime=None, capture=None,
                 transport=None, provider_factory=None, circuit=None,
                 budget=None, clock=None):
        self.store = store
        self.database = database
        self.runtime = runtime
        self.capture = capture if capture is not None else capture_screen
        self.transport = transport if transport is not None else getattr(store, 'ai_transport', None)
        self.circuit = circuit if circuit is not None else getattr(store, 'ai_circuit', None)
        if self.circuit is None:
            self.circuit = CircuitBreaker()
        self.budget = budget if budget is not None else BudgetLedger(database)
        self.provider_factory = provider_factory
        self.clock = clock if callable(clock) else time.time
        self._results = queue.Queue()
        self._active = {}
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._worker = None
        self._tick_registered = False
        self._closed = False
        self._last_proactive_capture = None
        self._last_proactive_check = None

    # -- public surface -------------------------------------------------

    def capture_available(self):
        """An injected capture seam counts as available on every platform."""
        return self.capture is not capture_screen or sys.platform == 'win32'

    def attach(self, ctx):
        if self._tick_registered:
            return
        register = getattr(ctx, 'register_tick', None)
        if register is None:
            return
        try:
            register(self.poll)
        except RuntimeError:
            return
        self._tick_registered = True

    def bind(self, runtime):
        """Startup wiring: proactive delivery target plus one tick drain.

        Shares the attach() guard, so a later command-side attach() does not
        register a second hook.
        """
        if self.runtime is None:
            self.runtime = runtime
        if self._tick_registered:
            return
        try:
            runtime.register_tick(self.poll)
        except (RuntimeError, TypeError):
            return
        self._tick_registered = True

    def submit(self, prompt=DEFAULT_PROMPT, monitor=None, ctx=None, kind='command'):
        """Validate and gate on the service thread, then start one worker.

        Raises VisionError for synchronous failures (disabled, not_available,
        busy, budget_exceeded, circuit_open, secrets codes). Provider and
        capture failures surface later as queued error events.
        """
        settings = self._settings()
        config = read_vision_settings(settings)
        if not config['enabled']:
            raise VisionError('not_available',
                              'Screenshot vision is disabled (vision.enabled).')
        if not self.capture_available():
            raise VisionError('not_available',
                              'Screen capture is only available on Windows.')
        with self._lock:
            if self._closed:
                raise VisionError('not_available', 'The vision service is closed.')
            if self._active:
                raise VisionError('busy', 'A vision analysis is already in progress.')
        ai_settings = settings.get('ai') if isinstance(settings.get('ai'), dict) else {}
        status = self.budget.status(ai_settings, self._reservation(ai_settings, prompt))
        if status.get('local_only'):
            raise VisionError('budget_exceeded',
                              'The vision request exceeds the %s limit.'
                              % (status.get('reason') or 'budget'))
        try:
            key = load_key_for(self.store)
        except SecretsError as exc:
            raise VisionError(exc.code, exc.message) from None
        if not self.circuit.allow():
            raise VisionError('circuit_open',
                              'The AI circuit is open; try again later.', True)
        request_id = 'vision-' + uuid.uuid4().hex
        active = {
            'request_id': request_id,
            'ctx': ctx,
            'kind': kind,
            'ai_settings': ai_settings,
        }
        worker = threading.Thread(target=self._run,
                                  args=(active, prompt, monitor, config, key),
                                  name='vision-' + request_id, daemon=True)
        with self._lock:
            self._active[request_id] = active
            self._worker = worker
        try:
            worker.start()
        except RuntimeError:
            with self._lock:
                self._active.pop(request_id, None)
                self._worker = None
            raise VisionError('internal_error',
                              'The vision request could not be started.', True) from None
        return request_id

    def maybe_capture(self, now=None, prompt=None, monitor=None):
        """Proactive capture-and-describe entry point for a scheduler tick.

        Returns the request id when a capture was started, else None. Only
        acts when vision.enabled is on and should_capture() says the interval
        elapsed; the attempt timestamp is recorded even when the call later
        fails, so a broken endpoint is not retried every tick.
        """
        if self._closed:
            return None
        config = read_vision_settings(self._settings())
        if not config['enabled'] or not self.capture_available():
            return None
        moment = float(self.clock()) if now is None else now
        if not should_capture(moment, self._last_proactive_capture,
                              config['proactive_interval_seconds']):
            return None
        self._last_proactive_capture = moment
        try:
            return self.submit(prompt=prompt or DEFAULT_PROMPT, monitor=monitor,
                               ctx=None, kind='proactive')
        except VisionError as exc:
            self._log('proactive_skipped', exc.code)
            return None

    def poll(self, now=None):
        """Drain finished work on the service thread (tick hook)."""
        if self._closed:
            return
        while True:
            try:
                result = self._results.get_nowait()
            except queue.Empty:
                return
            try:
                self._deliver(result)
            except Exception:
                self._log('deliver_failed')

    def tick(self, now=None):
        """Service-thread heartbeat: drain results, then consider one capture.

        ``now`` is the runtime's monotonic tick clock and is deliberately not
        forwarded: proactive intervals are measured against wall-clock time via
        ``self.clock``, and mixing the two bases would fire captures early or
        never. The proactive check itself is throttled to PROACTIVE_CHECK_SECONDS
        so a 100 ms tick does not deep-copy settings ten times a second.
        Everything here runs on the service thread, so the worker started by
        ``maybe_capture`` still hands its result back through ``poll``.
        """
        self.poll()
        if self._closed:
            return
        moment = float(self.clock())
        last = self._last_proactive_check
        if last is not None and moment - last < PROACTIVE_CHECK_SECONDS:
            return
        self._last_proactive_check = moment
        self.maybe_capture(now=moment)

    def close(self):
        """Cancel pending work; no new request starts after this call."""
        if self._closed:
            return
        self._closed = True
        self._cancel.set()
        with self._lock:
            worker = self._worker
        if worker is not None and worker.is_alive():
            try:
                worker.join(timeout=JOIN_TIMEOUT_SECONDS)
            except RuntimeError:
                pass

    def pending_count(self):
        with self._lock:
            return len(self._active)

    # -- internals -------------------------------------------------------

    def _settings(self):
        try:
            settings = self.store.settings
        except Exception:
            return {}
        return settings if isinstance(settings, dict) else {}

    @staticmethod
    def _reservation(ai_settings, prompt):
        """Worst-case tokens/cost: text reservation plus the image reserve."""
        text_messages = [{'role': 'system', 'content': SYSTEM_PROMPT},
                         {'role': 'user', 'content': prompt}]
        reserve = reservation(text_messages, ai_settings)
        try:
            input_price = float(ai_settings.get('input_price_per_million') or 0.0)
        except (TypeError, ValueError):
            input_price = 0.0
        reserve['tokens'] += IMAGE_TOKEN_RESERVE
        reserve['cost'] = round(reserve['cost']
                                + IMAGE_TOKEN_RESERVE / 1_000_000.0 * input_price, 10)
        return reserve

    def _provider(self, ai_settings, vision_model):
        if self.provider_factory is not None:
            provider = self.provider_factory(ai_settings)
        else:
            provider = OpenAICompatibleProvider(ai_settings, transport=self.transport)
        if vision_model:
            # The provider has no per-call model parameter; override the
            # instance attribute for this one-shot request.
            provider.model = vision_model
        return provider

    def _run(self, active, prompt, monitor, config, key):
        request_id = active['request_id']
        try:
            try:
                shot = self.capture(monitor=monitor,
                                    max_dimension=config['max_dimension'])
            except VisionError:
                raise
            except Exception:
                raise VisionError('capture_failed', 'Screen capture failed.') from None
            png = shot.get('png') if isinstance(shot, dict) else None
            if type(png) is not bytes or not png:
                raise VisionError('capture_failed', 'The capture returned no image.')
            messages = build_vision_messages(png, prompt)
            del png, shot  # the image is never persisted or logged
            provider = self._provider(active['ai_settings'], config['model'])
            completion = provider.stream_chat(messages, key, on_delta=None,
                                              cancel_event=self._cancel)
            text = completion.text if type(completion.text) is str else ''
            if not text.strip():
                raise ProviderError('invalid_response',
                                    'The provider returned no text.', True)
            self.circuit.record_success()
            result = {'kind': 'done', 'text': text.strip(),
                      'usage': completion.usage, 'model': completion.model}
        except ProviderError as exc:
            if exc.code == 'cancelled' or self._cancel.is_set():
                result = {'kind': 'cancelled'}
            else:
                self.circuit.record_failure(exc.code)
                result = {'kind': 'error', 'code': exc.code,
                          'retryable': exc.retryable, 'detail': exc.message}
        except VisionError as exc:
            result = {'kind': 'error', 'code': exc.code,
                      'retryable': exc.retryable, 'detail': exc.message}
        except Exception:
            result = {'kind': 'error', 'code': 'internal_error', 'retryable': False}
        finally:
            with self._lock:
                self._active.pop(request_id, None)
                self._worker = None
        if result['kind'] == 'cancelled' or self._cancel.is_set():
            return
        result['request_id'] = request_id
        result['ctx'] = active['ctx']
        result['source'] = active['kind']
        result['ai_settings'] = active['ai_settings']
        self._results.put(result)

    def _deliver(self, result):
        kind = result['kind']
        ai_settings = result.get('ai_settings') or {}
        ctx = result.get('ctx')
        if kind == 'done':
            usage = self.budget.record(result['request_id'], result.get('model', ''),
                                       result.get('usage') or {}, ai_settings)
            payload = {'text': result['text'], 'usage': usage,
                       'model': result.get('model', '')}
            self._log('done')
            if ctx is not None:
                _emit(ctx, 'vision.done', payload)
            elif self.runtime is not None and result.get('source') == 'proactive':
                self.runtime.post(PROACTIVE_REQUEST_ID, PROACTIVE_EVENT, payload)
            return
        if kind == 'error':
            code = str(result.get('code') or 'internal_error')
            payload = {'code': code, 'message': _message(code),
                       'retryable': bool(result.get('retryable', False))}
            if result.get('detail'):
                payload['detail'] = str(result['detail'])
            self._log('error', code)
            if ctx is not None:
                _emit(ctx, 'vision.error', payload)
            # Proactive failures stay in the diagnostic log; no bubble frame.

    def _log(self, code, message=''):
        """Event codes only; image data, prompts and keys never pass here."""
        events = getattr(self.runtime, 'events', None)
        record = getattr(events, 'record', None)
        if callable(record):
            try:
                record('info', 'vision', code, message)
            except Exception:
                pass


def _emit(ctx, event, payload):
    try:
        ctx.emit(event, payload)
        return
    except RuntimeError:
        pass
    except Exception:
        return
    try:
        ctx.post(event, payload)
    except Exception:
        pass


def get_vision_service(store, database=None, runtime=None):
    """Return the per-store vision service, creating it lazily like chat."""
    service = getattr(store, '_vision_service', None)
    if service is None:
        service = VisionService(store, database, runtime=runtime)
        store._vision_service = service
        return service
    if database is not None and service.database is None:
        service.database = database
        service.budget.database = database
    if runtime is not None and service.runtime is None:
        service.runtime = runtime
    return service


# --------------------------------------------------------------------------
# Command: vision.analyze
# --------------------------------------------------------------------------

def analyze(request, store, ctx):
    payload = request['payload']
    prompt = payload.get('prompt', DEFAULT_PROMPT)
    if type(prompt) is not str or len(prompt) > MAX_PROMPT_CHARS:
        raise ProtocolError('invalid_request',
                            'prompt must be a string of at most %d characters.' % MAX_PROMPT_CHARS,
                            request['id'])
    prompt = prompt.strip() or DEFAULT_PROMPT
    monitor = payload.get('monitor')
    if monitor is not None and (type(monitor) is not int
                                or not 0 <= monitor <= MAX_MONITOR_INDEX):
        raise ProtocolError('invalid_request',
                            'monitor must be an integer from 0 to %d.' % MAX_MONITOR_INDEX,
                            request['id'])
    database = getattr(ctx, 'database', None)
    if database is None:
        database = getattr(store, 'database', None)
    service = get_vision_service(store, database)
    service.attach(ctx)
    try:
        request_id = service.submit(prompt, monitor, ctx)
    except VisionError as exc:
        raise ProtocolError(exc.code, exc.message, request['id']) from None
    return 'accepted', {'request_id': request_id}


COMMANDS = {'vision.analyze': analyze}
