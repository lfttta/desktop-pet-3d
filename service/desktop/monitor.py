"""Read-only desktop attention sampling.

Only four signals are collected: the foreground window rectangle, the
foreground window class name, the shell notification state, and the last
input tick. Window titles, process names, screen contents, and keyboard or
mouse content are never read, returned, or logged.

The module is import-safe on every platform. All Win32 access is lazy and
wrapped so that a missing platform or a failing probe yields ``known=False``
instead of an exception.
"""

from datetime import datetime, timezone
import ctypes
import sys
import time

from service.behavior.budget import in_quiet_hours, parse_timestamp

VALID_STATES = (
    'unknown',
    'do_not_disturb',
    'fullscreen',
    'sleep_hours',
    'idle',
    'active_interaction',
    'active_work',
)

DEBOUNCED_KEYS = ('fullscreen', 'do_not_disturb', 'locked')

TICK_MASK = 0xFFFFFFFF

_UNKNOWN_SNAPSHOT = {
    'known': False,
    'fullscreen': False,
    'do_not_disturb': False,
    'locked': False,
    'idle_seconds': 0,
    'sampled_at': 0.0,
}

SNAPSHOT_KEY_ORDER = tuple(_UNKNOWN_SNAPSHOT)


def tick_delta_ms(last_tick, current_tick):
    """Milliseconds between two 32-bit tick values, wrapping on overflow.

    ``GetLastInputInfo`` reports a DWORD tick and ``GetTickCount64`` is
    truncated to its low 32 bits; the modular subtraction below is correct
    across the roughly 49.7 day wrap of that counter.
    """
    return (int(current_tick) - int(last_tick)) & TICK_MASK


def idle_seconds_from_ticks(last_tick, current_tick):
    """Whole idle seconds derived from two 32-bit tick values."""
    return tick_delta_ms(last_tick, current_tick) // 1000


def _coerce_idle(value):
    """Return a non-negative integer idle value, defaulting to zero."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, float) and value != value:
        return 0
    try:
        idle = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, idle)


def _coerce_number(value, default):
    """Return a finite float or ``default`` for unusable input."""
    if isinstance(value, bool):
        return float(default)
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(default)
    if number != number or number in (float('inf'), float('-inf')):
        return float(default)
    return number


def _local_now(value=None):
    """Resolve ``value`` to a wall-clock datetime in the local zone.

    A naive datetime is already a local wall-clock reading and is used as
    given; an aware datetime is converted to the system local zone; a number
    is treated as an epoch timestamp. A missing or unusable value means the
    current local time.
    """
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value
        return value.astimezone()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(float(value)).astimezone()
        except (OverflowError, OSError, ValueError):
            return datetime.now().astimezone()
    return datetime.now().astimezone()


class WindowsBackend:
    """Samples Win32 attention signals without touching window content.

    ``read`` returns ``None`` whenever the platform is unsupported, a Win32
    call fails, or the shell notification state is not one of the documented
    values. It never raises.
    """

    IGNORED_CLASS_NAMES = frozenset({'progman', 'workerw', 'shell_traywnd'})
    FULLSCREEN_TOLERANCE = 2
    MONITOR_DEFAULTTONEAREST = 2
    CLASS_NAME_LENGTH = 256

    # QUERY_USER_NOTIFICATION_STATE -> (do_not_disturb, locked).
    NOTIFICATION_STATES = {
        1: (True, True),    # QUNS_NOT_PRESENT: workstation locked.
        2: (True, False),   # QUNS_BUSY.
        3: (True, False),   # QUNS_RUNNING_D3D_FULL_SCREEN.
        4: (True, False),   # QUNS_PRESENTATION_MODE.
        5: (False, False),  # QUNS_ACCEPTS_NOTIFICATIONS.
        6: (True, False),   # QUNS_QUIET_TIME.
        7: (False, False),  # QUNS_APP.
    }

    def __init__(self):
        self._user32 = None
        self._shell32 = None
        self._kernel32 = None

    def read(self):
        """Return one raw reading, or ``None`` when no reading is possible."""
        try:
            return self._read()
        except Exception:
            return None

    def _read(self):
        if sys.platform != 'win32':
            return None
        user32, shell32, kernel32 = self._libraries()
        notification = self._notification_state(shell32)
        if notification is None:
            return None
        do_not_disturb, locked = notification
        idle_seconds = self._idle_seconds(user32, kernel32)
        if idle_seconds is None:
            return None
        return {
            'fullscreen': self._is_fullscreen(user32),
            'do_not_disturb': do_not_disturb,
            'locked': locked,
            'idle_seconds': idle_seconds,
        }

    def _libraries(self):
        if self._user32 is not None:
            return self._user32, self._shell32, self._kernel32
        user32 = ctypes.WinDLL('user32', use_last_error=True)
        shell32 = ctypes.WinDLL('shell32', use_last_error=True)
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)

        user32.GetForegroundWindow.restype = ctypes.c_void_p
        user32.GetForegroundWindow.argtypes = ()
        user32.GetWindowRect.restype = ctypes.c_int
        user32.GetWindowRect.argtypes = (ctypes.c_void_p, ctypes.POINTER(_Rect))
        user32.MonitorFromWindow.restype = ctypes.c_void_p
        user32.MonitorFromWindow.argtypes = (ctypes.c_void_p, ctypes.c_ulong)
        user32.GetMonitorInfoW.restype = ctypes.c_int
        user32.GetMonitorInfoW.argtypes = (ctypes.c_void_p,
                                           ctypes.POINTER(_MonitorInfo))
        user32.IsWindowVisible.restype = ctypes.c_int
        user32.IsWindowVisible.argtypes = (ctypes.c_void_p,)
        user32.GetClassNameW.restype = ctypes.c_int
        user32.GetClassNameW.argtypes = (ctypes.c_void_p,
                                         ctypes.POINTER(ctypes.c_wchar),
                                         ctypes.c_int)
        user32.GetLastInputInfo.restype = ctypes.c_int
        user32.GetLastInputInfo.argtypes = (ctypes.POINTER(_LastInputInfo),)

        shell32.SHQueryUserNotificationState.restype = ctypes.c_long
        shell32.SHQueryUserNotificationState.argtypes = (ctypes.POINTER(ctypes.c_int),)

        kernel32.GetTickCount64.restype = ctypes.c_ulonglong
        kernel32.GetTickCount64.argtypes = ()

        self._user32, self._shell32, self._kernel32 = user32, shell32, kernel32
        return user32, shell32, kernel32

    def _notification_state(self, shell32):
        state = ctypes.c_int(0)
        result = shell32.SHQueryUserNotificationState(ctypes.byref(state))
        if result != 0:
            return None
        return self.NOTIFICATION_STATES.get(int(state.value))

    def _idle_seconds(self, user32, kernel32):
        info = _LastInputInfo()
        info.cbSize = ctypes.sizeof(_LastInputInfo)
        if not user32.GetLastInputInfo(ctypes.byref(info)):
            return None
        current_tick = int(kernel32.GetTickCount64()) & TICK_MASK
        last_tick = int(info.dwTime) & TICK_MASK
        return idle_seconds_from_ticks(last_tick, current_tick)

    def _is_fullscreen(self, user32):
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return False
        if not user32.IsWindowVisible(hwnd):
            return False
        class_name = self._class_name(user32, hwnd)
        if class_name.lower() in self.IGNORED_CLASS_NAMES:
            return False
        rect = _Rect()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return False
        monitor = user32.MonitorFromWindow(hwnd, self.MONITOR_DEFAULTTONEAREST)
        if not monitor:
            return False
        info = _MonitorInfo()
        info.cbSize = ctypes.sizeof(_MonitorInfo)
        if not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            return False
        area = info.rcMonitor
        tolerance = self.FULLSCREEN_TOLERANCE
        return (rect.left <= area.left + tolerance
                and rect.top <= area.top + tolerance
                and rect.right >= area.right - tolerance
                and rect.bottom >= area.bottom - tolerance)

    def _class_name(self, user32, hwnd):
        buffer = ctypes.create_unicode_buffer(self.CLASS_NAME_LENGTH)
        length = user32.GetClassNameW(hwnd, buffer, self.CLASS_NAME_LENGTH)
        if length <= 0:
            return ''
        return buffer.value


class _Rect(ctypes.Structure):
    _fields_ = (('left', ctypes.c_long), ('top', ctypes.c_long),
                ('right', ctypes.c_long), ('bottom', ctypes.c_long))


class _MonitorInfo(ctypes.Structure):
    _fields_ = (('cbSize', ctypes.c_ulong), ('rcMonitor', _Rect),
                ('rcWork', _Rect), ('dwFlags', ctypes.c_ulong))


class _LastInputInfo(ctypes.Structure):
    _fields_ = (('cbSize', ctypes.c_uint), ('dwTime', ctypes.c_uint))


class DesktopMonitor:
    """Debounced, poll-limited reader for desktop attention signals.

    The backend is consulted at most once per ``poll_seconds``. The
    ``fullscreen``, ``do_not_disturb`` and ``locked`` flags only change after
    ``samples`` consecutive readings agree; ``idle_seconds`` is passed
    through unchanged. A failed reading is cached for the same window and is
    reported as ``known=False`` with every other field at its zero value.
    """

    def __init__(self, backend=None, poll_seconds=3.0, samples=2, clock=None):
        self._backend = backend if backend is not None else WindowsBackend()
        self._poll_seconds = max(0.0, _coerce_number(poll_seconds, 3.0))
        try:
            self._samples = max(1, int(samples))
        except (TypeError, ValueError, OverflowError):
            self._samples = 1
        if clock is not None and not callable(clock):
            raise TypeError('clock must be callable')
        self._clock = clock if clock is not None else time.time
        self._last_read_at = None
        self._cached = None
        self._stable = {key: False for key in DEBOUNCED_KEYS}
        self._pending = {key: None for key in DEBOUNCED_KEYS}

    def snapshot(self, now=None):
        """Return the fixed-shape snapshot described by ``SNAPSHOT_KEY_ORDER``.

        ``now`` optionally overrides the recorded ``sampled_at`` (a datetime
        or epoch seconds); poll eligibility is always driven by ``clock``.
        A cached snapshot keeps the timestamp of the reading it came from.
        """
        try:
            return self._snapshot(now)
        except Exception:
            self._reset_debounce()
            self._cached = None
            self._last_read_at = None
            return _unknown_snapshot()

    def _snapshot(self, now):
        reference = float(self._clock())
        sample_time = reference if now is None else self._resolve_time(now, reference)
        if self._cached is not None:
            if reference - self._last_read_at < self._poll_seconds:
                return dict(self._cached)
        self._last_read_at = reference
        raw = None
        if self._backend is not None:
            try:
                raw = self._backend.read()
            except Exception:
                raw = None
        if not isinstance(raw, dict):
            self._reset_debounce()
            result = _unknown_snapshot()
        else:
            flags = self._debounce(raw)
            result = {
                'known': True,
                'fullscreen': flags['fullscreen'],
                'do_not_disturb': flags['do_not_disturb'],
                'locked': flags['locked'],
                'idle_seconds': _coerce_idle(raw.get('idle_seconds')),
                'sampled_at': float(sample_time),
            }
        self._cached = result
        return dict(result)

    @staticmethod
    def _resolve_time(value, fallback):
        if isinstance(value, datetime):
            try:
                return float(value.timestamp())
            except (OverflowError, OSError, ValueError):
                return float(fallback)
        if isinstance(value, bool):
            return float(fallback)
        try:
            resolved = float(value)
        except (TypeError, ValueError):
            return float(fallback)
        if resolved != resolved or resolved in (float('inf'), float('-inf')):
            return float(fallback)
        return resolved

    def _debounce(self, raw):
        """Advance the per-flag confirmation counters by one reading."""
        confirmed = {}
        for key in DEBOUNCED_KEYS:
            value = bool(raw.get(key, False))
            if value == self._stable[key]:
                self._pending[key] = None
                confirmed[key] = self._stable[key]
                continue
            pending = self._pending[key]
            if pending is None or pending[0] != value:
                pending = [value, 1]
                self._pending[key] = pending
            else:
                pending[1] += 1
            if pending[1] >= self._samples:
                self._stable[key] = value
                self._pending[key] = None
            confirmed[key] = self._stable[key]
        return confirmed

    def _reset_debounce(self):
        for key in DEBOUNCED_KEYS:
            self._stable[key] = False
            self._pending[key] = None

    def close(self):
        """Release the backend and cached state. Safe to call repeatedly."""
        self._backend = None
        self._cached = None
        self._last_read_at = None
        self._reset_debounce()


def _unknown_snapshot():
    """A fresh copy of the fixed unknown shape, in declaration order."""
    return dict(_UNKNOWN_SNAPSHOT)


def classify(snapshot, quiet_start='23:00', quiet_end='08:00',
             idle_after_seconds=300, now=None, last_user_at='',
             interaction_seconds=120):
    """Map one snapshot onto a single attention state.

    Precedence: unknown, do_not_disturb (a locked workstation reports
    ``reason='locked'``), fullscreen, sleep_hours, idle, active_interaction,
    active_work. ``now`` is evaluated on the local wall clock; ``last_user_at``
    is an ISO timestamp and an unparsable value counts as no recorded activity.
    """
    if not isinstance(snapshot, dict) or not snapshot.get('known'):
        return {'state': 'unknown', 'reason': 'unknown'}
    if bool(snapshot.get('locked')):
        return {'state': 'do_not_disturb', 'reason': 'locked'}
    if bool(snapshot.get('do_not_disturb')):
        return {'state': 'do_not_disturb', 'reason': 'do_not_disturb'}
    if bool(snapshot.get('fullscreen')):
        return {'state': 'fullscreen', 'reason': 'fullscreen'}

    local_now = _local_now(now)
    if in_quiet_hours(local_now, quiet_start, quiet_end):
        return {'state': 'sleep_hours', 'reason': 'quiet_hours'}

    idle_limit = max(0.0, _coerce_number(idle_after_seconds, 300))
    if _coerce_idle(snapshot.get('idle_seconds')) >= idle_limit:
        return {'state': 'idle', 'reason': 'idle'}

    last_at = parse_timestamp(last_user_at)
    if last_at is not None:
        recent_limit = _coerce_number(interaction_seconds, 120)
        elapsed = (local_now.astimezone(timezone.utc) - last_at).total_seconds()
        if elapsed <= recent_limit:
            return {'state': 'active_interaction', 'reason': 'recent_interaction'}

    return {'state': 'active_work', 'reason': 'active_work'}
