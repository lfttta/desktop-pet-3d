"""Desktop attention monitor contracts.

Every test uses an injected fake backend; no Win32 function is called.
"""

from datetime import datetime, timedelta, timezone
import inspect

import pytest

from service.desktop import monitor as monitor_module
from service.desktop.monitor import (
    VALID_STATES,
    DesktopMonitor,
    WindowsBackend,
    classify,
    idle_seconds_from_ticks,
    tick_delta_ms,
)

SNAPSHOT_KEYS = {
    'known', 'fullscreen', 'do_not_disturb', 'locked', 'idle_seconds', 'sampled_at',
}

NOW = datetime(2026, 11, 6, 12, 0)        # local wall clock, outside quiet hours
QUIET = datetime(2026, 11, 6, 23, 30)     # inside 23:00-08:00
MORNING = datetime(2026, 11, 6, 7, 30)    # still inside 23:00-08:00


def iso_ago(now, seconds):
    """ISO UTC timestamp for ``seconds`` before the local wall clock ``now``."""
    return (now.astimezone(timezone.utc) - timedelta(seconds=seconds)).isoformat()


def reading(fullscreen=False, do_not_disturb=False, locked=False, idle_seconds=0):
    return {
        'fullscreen': fullscreen,
        'do_not_disturb': do_not_disturb,
        'locked': locked,
        'idle_seconds': idle_seconds,
    }


def snap(known=True, **overrides):
    value = {
        'known': known,
        'fullscreen': False,
        'do_not_disturb': False,
        'locked': False,
        'idle_seconds': 0,
        'sampled_at': 0.0,
    }
    value.update(overrides)
    return value


class FakeClock:
    def __init__(self, value=1000.0):
        self.value = float(value)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class FixedBackend:
    """Returns the same reading on every call and counts the calls."""

    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error
        self.calls = 0

    def read(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.payload


class ScriptedBackend:
    """Returns queued readings in order; ``None`` once the queue is empty."""

    def __init__(self, readings):
        self.readings = list(readings)
        self.calls = 0

    def read(self):
        self.calls += 1
        if not self.readings:
            return None
        return self.readings.pop(0)


def build(readings, poll_seconds=0.0, samples=2, clock=None):
    backend = ScriptedBackend(readings)
    monitor = DesktopMonitor(backend=backend, poll_seconds=poll_seconds,
                             samples=samples, clock=clock or FakeClock())
    return monitor, backend


# ---------------------------------------------------------------- snapshot shape


def test_snapshot_key_set_is_fixed_and_carries_no_private_data():
    monitor, _ = build([reading()], samples=1)
    snapshot = monitor.snapshot()
    assert set(snapshot) == SNAPSHOT_KEYS
    assert type(snapshot['known']) is bool
    assert type(snapshot['fullscreen']) is bool
    assert type(snapshot['do_not_disturb']) is bool
    assert type(snapshot['locked']) is bool
    assert type(snapshot['idle_seconds']) is int
    assert type(snapshot['sampled_at']) is float
    for key in snapshot:
        lowered = key.lower()
        for forbidden in ('title', 'process', 'exe', 'path', 'window', 'text', 'content'):
            assert forbidden not in lowered


def test_unknown_snapshot_uses_the_same_fixed_shape():
    monitor, _ = build([None])
    snapshot = monitor.snapshot()
    assert set(snapshot) == SNAPSHOT_KEYS
    assert snapshot['known'] is False
    assert snapshot['fullscreen'] is False
    assert snapshot['do_not_disturb'] is False
    assert snapshot['locked'] is False
    assert snapshot['idle_seconds'] == 0


def test_module_never_reaches_for_window_text_or_process_identifiers():
    source = inspect.getsource(monitor_module)
    for token in ('GetWindowText', 'GetWindowThreadProcessId', 'QueryFullProcessImageName',
                  'GetProcessImageFileName', 'OpenProcess'):
        assert token not in source


# ---------------------------------------------------------------------- debounce


def test_single_differing_sample_does_not_switch_and_two_consecutive_do():
    monitor, _ = build([
        reading(fullscreen=False),
        reading(fullscreen=True),
        reading(fullscreen=True),
    ])
    assert monitor.snapshot()['fullscreen'] is False
    assert monitor.snapshot()['fullscreen'] is False   # one deviation is not enough
    assert monitor.snapshot()['fullscreen'] is True    # second consecutive agrees


def test_debounce_also_applies_when_switching_back():
    monitor, _ = build([
        reading(fullscreen=True),
        reading(fullscreen=True),
        reading(fullscreen=False),
        reading(fullscreen=True),
        reading(fullscreen=False),
        reading(fullscreen=False),
    ])
    assert monitor.snapshot()['fullscreen'] is False
    assert monitor.snapshot()['fullscreen'] is True
    assert monitor.snapshot()['fullscreen'] is True    # one False does not switch back
    assert monitor.snapshot()['fullscreen'] is True    # a True in between restarts the count
    assert monitor.snapshot()['fullscreen'] is True    # first False of the new run
    assert monitor.snapshot()['fullscreen'] is False   # second consecutive False


def test_do_not_disturb_and_locked_are_debounced_independently():
    monitor, _ = build([
        reading(do_not_disturb=False, locked=False, fullscreen=False),
        reading(do_not_disturb=True, locked=False, fullscreen=True),
        reading(do_not_disturb=True, locked=True, fullscreen=True),
        reading(do_not_disturb=True, locked=True, fullscreen=True),
    ])
    first = monitor.snapshot()
    assert first['known'] is True
    assert first['do_not_disturb'] is False
    assert first['fullscreen'] is False
    assert first['locked'] is False
    second = monitor.snapshot()
    assert second['do_not_disturb'] is False   # only one True sample so far
    assert second['fullscreen'] is False       # only one True sample so far
    assert second['locked'] is False
    third = monitor.snapshot()
    assert third['do_not_disturb'] is True     # second consecutive True sample
    assert third['fullscreen'] is True
    assert third['locked'] is False            # locked has only one True sample
    fourth = monitor.snapshot()
    assert fourth['locked'] is True            # second consecutive True sample
    assert fourth['do_not_disturb'] is True
    assert fourth['fullscreen'] is True


def test_idle_seconds_is_not_debounced():
    monitor, _ = build([
        reading(idle_seconds=0),
        reading(idle_seconds=5000),
        reading(idle_seconds=1),
    ], samples=2)
    assert monitor.snapshot()['idle_seconds'] == 0
    assert monitor.snapshot()['idle_seconds'] == 5000
    assert monitor.snapshot()['idle_seconds'] == 1


# ---------------------------------------------------------------- backend failure


def test_raising_backend_yields_known_false_and_no_propagation():
    monitor, _ = build([RuntimeError('boom')])
    snapshot = monitor.snapshot()
    assert snapshot['known'] is False
    assert snapshot['fullscreen'] is False
    assert snapshot['do_not_disturb'] is False
    assert snapshot['locked'] is False
    assert snapshot['idle_seconds'] == 0
    assert snapshot['sampled_at'] == 0.0


def test_backend_returning_none_yields_known_false():
    monitor, _ = build([None])
    snapshot = monitor.snapshot()
    assert snapshot['known'] is False
    assert snapshot['fullscreen'] is False
    assert snapshot['idle_seconds'] == 0


@pytest.mark.parametrize('payload', [None, [], 'fullscreen', 3.5, True])
def test_backend_payload_that_is_not_a_dict_yields_known_false(payload):
    monitor, _ = build([payload])
    assert monitor.snapshot()['known'] is False


def test_recovery_after_failure_restarts_the_debounce_window():
    monitor, _ = build([RuntimeError('boom'), reading(fullscreen=True),
                        reading(fullscreen=True)], samples=2)
    assert monitor.snapshot()['known'] is False
    recovered = monitor.snapshot()
    assert recovered['known'] is True
    assert recovered['fullscreen'] is False   # failure cleared the stable flags
    assert monitor.snapshot()['fullscreen'] is True


def test_close_is_idempotent_and_stops_reading_the_backend():
    backend = FixedBackend(reading(fullscreen=True))
    monitor = DesktopMonitor(backend=backend, poll_seconds=0.0, samples=1,
                             clock=FakeClock())
    monitor.snapshot()
    assert backend.calls == 1
    monitor.close()
    monitor.close()
    after = monitor.snapshot()
    assert backend.calls == 1
    assert after['known'] is False
    assert after['fullscreen'] is False


def test_windows_backend_read_returns_none_instead_of_raising(monkeypatch):
    backend = WindowsBackend()

    def unavailable():
        raise OSError('user32 missing')

    monkeypatch.setattr(backend, '_libraries', unavailable)
    assert backend.read() is None


# ------------------------------------------------------------------------- cache


def test_repeated_snapshot_within_poll_window_reads_the_backend_once():
    clock = FakeClock(1000.0)
    backend = FixedBackend(reading())
    monitor = DesktopMonitor(backend=backend, poll_seconds=3.0, samples=1, clock=clock)
    first = monitor.snapshot()
    clock.advance(1.0)
    second = monitor.snapshot()
    clock.advance(1.9)
    third = monitor.snapshot()
    assert backend.calls == 1
    assert first == second == third
    assert second is not first
    clock.advance(0.2)   # 3.1 seconds since the only read
    monitor.snapshot()
    assert backend.calls == 2


def test_cached_snapshot_cannot_be_mutated_by_the_caller():
    clock = FakeClock(1000.0)
    backend = FixedBackend(reading(fullscreen=True))
    monitor = DesktopMonitor(backend=backend, poll_seconds=30.0, samples=1, clock=clock)
    first = monitor.snapshot()
    first['fullscreen'] = 'tampered'
    first['idle_seconds'] = -1
    again = monitor.snapshot()
    assert again['fullscreen'] is True
    assert again['idle_seconds'] == 0
    assert backend.calls == 1


def test_sampled_at_follows_the_injected_clock_and_explicit_now():
    clock = FakeClock(1000.0)
    monitor = DesktopMonitor(backend=FixedBackend(reading()), poll_seconds=0.0,
                             samples=1, clock=clock)
    assert monitor.snapshot()['sampled_at'] == 1000.0
    explicit = datetime(2026, 1, 2, 3, 4, 5)
    assert monitor.snapshot(now=explicit)['sampled_at'] == explicit.timestamp()


def test_declared_key_order_matches_the_snapshot():
    assert tuple(monitor_module.SNAPSHOT_KEY_ORDER) == (
        'known', 'fullscreen', 'do_not_disturb', 'locked', 'idle_seconds', 'sampled_at')


@pytest.mark.parametrize('idle', [None, 'oops', -5, float('nan'), float('inf')])
def test_unusable_idle_values_normalize_to_zero(idle):
    monitor, _ = build([reading(idle_seconds=idle)], samples=1)
    snapshot = monitor.snapshot()
    assert snapshot['known'] is True
    assert snapshot['idle_seconds'] == 0


@pytest.mark.parametrize('now_value', [None, 'later', float('inf')])
def test_unusable_now_falls_back_to_the_injected_clock(now_value):
    clock = FakeClock(1000.0)
    monitor = DesktopMonitor(backend=FixedBackend(reading()), poll_seconds=0.0,
                             samples=1, clock=clock)
    assert monitor.snapshot(now=now_value)['sampled_at'] == 1000.0


# ------------------------------------------------------------------- tick wrap


def test_tick_delta_wraps_across_the_32_bit_boundary():
    assert tick_delta_ms(0xFFFFFF00, 0x00000100) == 0x200
    assert tick_delta_ms(0, 0xFFFFFFFF) == 0xFFFFFFFF
    assert tick_delta_ms(1000, 61000) == 60000


def test_idle_seconds_from_ticks_handles_wrap():
    assert idle_seconds_from_ticks(0xFFFFFF00, 0x00000100) == 0
    assert idle_seconds_from_ticks(0xFFFFFE00, 0x00000200) == 1
    assert idle_seconds_from_ticks(0xFFFFFF9C, 0x00000000) == 0
    # 0xFFFF ms remain before the wrap plus 0x10000 ms after it.
    assert idle_seconds_from_ticks(0xFFFF0000, 0x00010000) == 131


class _TickUser32:
    def __init__(self, last_tick):
        self.last_tick = last_tick

    def GetLastInputInfo(self, pointer):
        pointer._obj.dwTime = self.last_tick
        return 1


class _TickKernel32:
    def __init__(self, current_tick):
        self.current_tick = current_tick

    def GetTickCount64(self):
        return self.current_tick


def test_windows_backend_idle_uses_wrapped_low_32_bits():
    backend = WindowsBackend()
    wrapped = backend._idle_seconds(_TickUser32(0xFFFFFF00), _TickKernel32(0x00000100))
    assert wrapped == 0
    crossing = backend._idle_seconds(_TickUser32(0xFFFFFE00), _TickKernel32(0x00000200))
    assert crossing == 1
    plain = backend._idle_seconds(_TickUser32(1000), _TickKernel32(121000))
    assert plain == 120


# --------------------------------------------------------------------- classify


def test_classify_rejects_non_snapshot_input_as_unknown():
    for value in (None, [], 'snapshot', 3, snap(known=False)):
        result = classify(value, now=NOW)
        assert result == {'state': 'unknown', 'reason': 'unknown'}


def test_classify_returns_only_state_and_reason_and_uses_known_states():
    result = classify(snap(), now=NOW)
    assert set(result) == {'state', 'reason'}
    assert result['state'] in VALID_STATES
    assert set(VALID_STATES) == {
        'unknown', 'do_not_disturb', 'fullscreen', 'sleep_hours',
        'idle', 'active_interaction', 'active_work',
    }


def test_locked_is_reported_as_do_not_disturb_with_locked_reason():
    result = classify(snap(locked=True), now=NOW)
    assert result == {'state': 'do_not_disturb', 'reason': 'locked'}


def test_do_not_disturb_state():
    assert classify(snap(do_not_disturb=True), now=NOW) == {
        'state': 'do_not_disturb', 'reason': 'do_not_disturb'}


def test_do_not_disturb_outranks_fullscreen():
    result = classify(snap(do_not_disturb=True, fullscreen=True), now=NOW)
    assert result['state'] == 'do_not_disturb'


def test_locked_outranks_do_not_disturb_and_fullscreen():
    result = classify(snap(locked=True, do_not_disturb=True, fullscreen=True), now=NOW)
    assert result == {'state': 'do_not_disturb', 'reason': 'locked'}


def test_fullscreen_outranks_quiet_hours_and_idle():
    result = classify(snap(fullscreen=True, idle_seconds=9999), now=QUIET)
    assert result == {'state': 'fullscreen', 'reason': 'fullscreen'}


def test_quiet_hours_outrank_idle_and_interaction():
    result = classify(snap(idle_seconds=9999, do_not_disturb=False), now=QUIET,
                      last_user_at=iso_ago(QUIET, 5))
    assert result == {'state': 'sleep_hours', 'reason': 'quiet_hours'}
    assert classify(snap(), now=MORNING)['state'] == 'sleep_hours'


@pytest.mark.parametrize('moment, expected', [
    (datetime(2026, 11, 6, 22, 59), 'active_work'),
    (datetime(2026, 11, 6, 23, 0), 'sleep_hours'),
    (datetime(2026, 11, 7, 7, 59), 'sleep_hours'),
    (datetime(2026, 11, 7, 8, 0), 'active_work'),
    (datetime(2026, 11, 7, 12, 0), 'active_work'),
])
def test_quiet_window_boundaries_follow_budget_semantics(moment, expected):
    assert classify(snap(), now=moment)['state'] == expected


def test_empty_quiet_window_disables_sleep_hours():
    result = classify(snap(), quiet_start='', quiet_end='', now=QUIET)
    assert result['state'] == 'active_work'


def test_idle_threshold_and_passthrough():
    assert classify(snap(idle_seconds=299), now=NOW)['state'] == 'active_work'
    assert classify(snap(idle_seconds=300), now=NOW) == {
        'state': 'idle', 'reason': 'idle'}
    assert classify(snap(idle_seconds=60), now=NOW, idle_after_seconds=60)['state'] == 'idle'
    assert classify(snap(idle_seconds=59), now=NOW, idle_after_seconds=60)['state'] == 'active_work'


def test_idle_outranks_active_interaction():
    result = classify(snap(idle_seconds=900), now=NOW,
                      last_user_at=iso_ago(NOW, 10), interaction_seconds=120)
    assert result == {'state': 'idle', 'reason': 'idle'}


def test_recent_user_activity_is_active_interaction():
    result = classify(snap(idle_seconds=1), now=NOW, last_user_at=iso_ago(NOW, 60))
    assert result == {'state': 'active_interaction', 'reason': 'recent_interaction'}
    boundary = classify(snap(idle_seconds=1), now=NOW,
                        last_user_at=iso_ago(NOW, 120), interaction_seconds=120)
    assert boundary['state'] == 'active_interaction'
    stale = classify(snap(idle_seconds=1), now=NOW,
                     last_user_at=iso_ago(NOW, 121), interaction_seconds=120)
    assert stale == {'state': 'active_work', 'reason': 'active_work'}


@pytest.mark.parametrize('last_user_at', ['', 'not-a-timestamp', '2026-13-40T99:99:99', None, 120])
def test_unparsable_last_user_activity_counts_as_no_activity(last_user_at):
    result = classify(snap(idle_seconds=1), now=NOW, last_user_at=last_user_at)
    assert result == {'state': 'active_work', 'reason': 'active_work'}


def test_active_work_is_the_default_state():
    assert classify(snap(), now=NOW) == {'state': 'active_work', 'reason': 'active_work'}


def test_classify_does_not_mutate_the_snapshot():
    snapshot = snap(fullscreen=True)
    before = dict(snapshot)
    classify(snapshot, now=QUIET)
    assert snapshot == before
