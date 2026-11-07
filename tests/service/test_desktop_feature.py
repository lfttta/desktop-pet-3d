"""Desktop feature contracts: status payload, policy and command wiring."""

import io
from types import SimpleNamespace

import pytest

from service import commands
from service.commands import dispatch
from service.database import Database
from service.features import autonomy, desktop
from service.protocol import ProtocolError
from service.settings import SettingsStore

FULLSCREEN_SNAPSHOT = {
    'known': True,
    'fullscreen': True,
    'do_not_disturb': False,
    'locked': False,
    'idle_seconds': 0,
    'sampled_at': 1234.5,
}

UNKNOWN_SNAPSHOT = {
    'known': False,
    'fullscreen': False,
    'do_not_disturb': False,
    'locked': False,
    'idle_seconds': 0,
    'sampled_at': 0.0,
}


class FakeMonitor:
    def __init__(self, snapshot=None, error=None):
        self.snapshot_value = dict(FULLSCREEN_SNAPSHOT) if snapshot is None else snapshot
        self.error = error
        self.calls = 0

    def snapshot(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return dict(self.snapshot_value)


@pytest.fixture
def database(tmp_path):
    db = Database(tmp_path / 'pet.db')
    try:
        yield db
    finally:
        db.close()


def _store(tmp_path, monitor):
    store = SettingsStore(tmp_path)
    store.desktop_monitor = monitor
    return store


def test_desktop_status_reports_state_snapshot_and_policy(tmp_path, database):
    monitor = FakeMonitor()
    store = _store(tmp_path, monitor)
    ctx = SimpleNamespace(database=database)

    event, payload = desktop.status({'id': 'd', 'payload': {}}, store, ctx)
    assert event == 'result'
    assert payload['enabled'] is True
    assert payload['snapshot'] == FULLSCREEN_SNAPSHOT
    assert payload['state'] == 'fullscreen'
    assert payload['reason'] == 'fullscreen'
    assert payload['policy'] == {
        'allowed': False,
        'reason': 'fullscreen',
        'categories': [],
        'min_factor': 1.0,
        'max_per_hour': None,
        'state': 'fullscreen',
    }
    assert monitor.calls == 1

    # The policy follows the configured relaxation for that state.
    store.update({'desktop': {'fullscreen_policy': 'minimal'}})
    _, relaxed = desktop.status({'id': 'd2', 'payload': {}}, store, ctx)
    assert relaxed['state'] == 'fullscreen'
    assert relaxed['policy']['allowed'] is True
    assert relaxed['policy']['categories'] == ['care']
    assert relaxed['policy']['reason'] == 'allowed'


def test_desktop_monitor_is_created_lazily_and_cached(tmp_path, database, monkeypatch):
    created = []

    class RecordingMonitor:
        def __init__(self, poll_seconds=3.0):
            self.poll_seconds = poll_seconds
            created.append(self)

        def snapshot(self):
            return dict(UNKNOWN_SNAPSHOT)

    # Never touch Win32 from a unit test; the monitor factory is swapped out.
    monkeypatch.setattr(desktop, 'DesktopMonitor', RecordingMonitor)
    store = SettingsStore(tmp_path)
    store.update({'desktop': {'poll_seconds': 11}})
    ctx = SimpleNamespace(database=database)
    assert getattr(store, 'desktop_monitor', None) is None

    first = desktop.current(store, ctx)
    assert first['enabled'] is True
    assert len(created) == 1
    assert created[0].poll_seconds == 11
    desktop.current(store, ctx)
    assert len(created) == 1
    assert store.desktop_monitor is created[0]


def test_disabled_desktop_reports_unknown_without_sampling(tmp_path, database):
    monitor = FakeMonitor()
    store = _store(tmp_path, monitor)
    store.update({'desktop': {'enabled': False}})
    ctx = SimpleNamespace(database=database)

    event, payload = desktop.status({'id': 'd', 'payload': {}}, store, ctx)
    assert event == 'result'
    assert payload['enabled'] is False
    assert payload['snapshot'] == UNKNOWN_SNAPSHOT
    assert payload['state'] == 'unknown'
    assert payload['reason'] == 'unknown'
    assert payload['policy']['allowed'] is True
    assert payload['policy']['reason'] == 'desktop_unknown'
    assert monitor.calls == 0
    # The cached sampler is released, not left alive behind a disabled switch.
    assert getattr(store, 'desktop_monitor', None) is None


def test_desktop_status_uses_autonomy_quiet_hours(tmp_path, database, monkeypatch):
    """quiet_start/quiet_end live in the autonomy group, not the desktop one."""
    calls = []

    def fake_classify(snapshot, quiet_start='23:00', quiet_end='08:00',
                      idle_after_seconds=300, now=None, last_user_at='',
                      interaction_seconds=120):
        calls.append({'quiet_start': quiet_start, 'quiet_end': quiet_end,
                      'idle_after_seconds': idle_after_seconds})
        return {'state': 'active_interaction', 'reason': 'test'}

    monkeypatch.setattr(desktop, 'classify', fake_classify)
    store = _store(tmp_path, FakeMonitor())
    store.update({'autonomy': {'quiet_start': '01:15', 'quiet_end': '02:30'},
                  'desktop': {'idle_after_seconds': 600}})
    ctx = SimpleNamespace(database=database)

    desktop.current(store, ctx)
    assert calls == [{'quiet_start': '01:15', 'quiet_end': '02:30',
                      'idle_after_seconds': 600}]


def test_monitor_failure_degrades_to_unknown(tmp_path, database):
    monitor = FakeMonitor(error=RuntimeError('probe failed'))
    store = _store(tmp_path, monitor)
    ctx = SimpleNamespace(database=database)

    event, payload = desktop.status({'id': 'd', 'payload': {}}, store, ctx)
    assert event == 'result'
    # A transient sampling failure is not the user's switch: the App must not
    # render it as "desktop sensing is off".
    assert payload['enabled'] is True
    assert payload['snapshot'] == UNKNOWN_SNAPSHOT
    assert payload['state'] == 'unknown'
    assert payload['reason'] == 'sampling_failed'
    assert payload['policy']['reason'] == 'desktop_unknown'
    assert monitor.calls == 1


def test_unreadable_snapshot_is_reported_as_sampling_failure(tmp_path, database):
    monitor = FakeMonitor(snapshot=dict(UNKNOWN_SNAPSHOT))
    store = _store(tmp_path, monitor)
    ctx = SimpleNamespace(database=database)

    _, payload = desktop.status({'id': 'd', 'payload': {}}, store, ctx)
    assert payload['enabled'] is True
    assert payload['reason'] == 'sampling_failed'
    assert payload['state'] == 'unknown'


def test_autonomy_get_includes_desktop_status(tmp_path, database):
    monitor = FakeMonitor()
    store = _store(tmp_path, monitor)
    ctx = SimpleNamespace(database=database)

    event, payload = autonomy.get({'id': 'a', 'payload': {}}, store, ctx)
    assert event == 'result'
    assert set(payload) == {'autonomy', 'status', 'desktop'}
    assert payload['desktop'] == desktop.current(store, ctx)
    assert payload['desktop']['state'] == 'fullscreen'
    assert monitor.calls == 2


def test_desktop_status_is_a_registered_command_not_a_reserved_prefix(tmp_path):
    assert 'desktop' not in commands.RESERVED_PREFIXES
    assert 'desktop.status' in commands.HANDLERS
    assert desktop.NAME == 'desktop'
    assert desktop.CAPABILITY == {
        'available': True,
        'reason': '',
        'commands': ['desktop.status'],
    }
    store = SettingsStore(tmp_path)
    store.desktop_monitor = FakeMonitor()
    event, payload = dispatch({'type': 'desktop.status', 'id': 'r', 'payload': {}}, store)
    assert event == 'result'
    assert payload['state'] == 'fullscreen'
    # Other desktop commands are no longer claimed as reserved.
    with pytest.raises(ProtocolError) as caught:
        dispatch({'type': 'desktop.notify', 'id': 'r2', 'payload': {}}, store)
    assert caught.value.code == 'unknown_command'


def _install_fake_scheduler(monkeypatch):
    from service.behavior import proactive
    created = []

    class FakeScheduler:
        def __init__(self, runtime, store, database):
            self.runtime = runtime
            self.store = store
            self.database = database
            self.closed = 0
            created.append(self)

        def tick(self, now=None):
            pass

        def close(self):
            self.closed += 1

    monkeypatch.setattr(proactive, 'ProactiveScheduler', FakeScheduler)
    return created


def test_serve_registers_and_closes_the_proactive_scheduler(tmp_path, monkeypatch):
    from service.runtime import serve
    created = _install_fake_scheduler(monkeypatch)
    output, errors = io.BytesIO(), io.BytesIO()
    assert serve(tmp_path, io.BytesIO(b''), output, errors) == 0
    assert len(created) == 1
    assert created[0].closed == 1
    assert created[0].tick in created[0].runtime._tick_hooks


def test_serve_closes_the_scheduler_when_the_loop_raises(tmp_path, monkeypatch):
    from service import runtime as runtime_module
    created = _install_fake_scheduler(monkeypatch)

    def explode(self, input_stream):
        raise RuntimeError('loop failed')

    monkeypatch.setattr(runtime_module.Runtime, 'run', explode)
    with pytest.raises(RuntimeError):
        runtime_module.serve(tmp_path, io.BytesIO(b''), io.BytesIO(), io.BytesIO())
    assert len(created) == 1
    assert created[0].closed == 1
