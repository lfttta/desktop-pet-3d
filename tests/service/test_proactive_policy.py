"""Desktop-aware proactive policy and budget tightening contracts."""

from datetime import datetime, timedelta, timezone
import json
import sys
import time
import types

import pytest

from service.behavior import budget, proactive
from service.database import Database

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)

DESKTOP_ON = {
    'enabled': True,
    'fullscreen_policy': 'silent',
    'dnd_policy': 'silent',
    'work_focus_policy': 'normal',
    'idle_policy': 'normal',
}


def _desktop(**overrides):
    value = dict(DESKTOP_ON)
    value.update(overrides)
    return value


def _config(**overrides):
    config = {
        'enabled': True,
        'quiet_start': '00:00',
        'quiet_end': '00:00',
        'min_seconds_between': 600,
        'max_per_hour': 4,
        'manual_quiet_until': '',
    }
    config.update(overrides)
    return config


@pytest.fixture
def database(tmp_path):
    db = Database(tmp_path / 'pet.db')
    try:
        yield db
    finally:
        db.close()


def _write_phrase_override(directory):
    path = directory / 'expression' / 'phrases.local.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        'version': 1,
        'phrases': [
            {'id': 'test_care_a', 'text': '要不要歇一会儿？', 'category': 'care',
             'cooldown_seconds': 0, 'daily_limit': 20},
            {'id': 'test_care_b', 'text': '记得喝点水。', 'category': 'care',
             'cooldown_seconds': 0, 'daily_limit': 20},
            {'id': 'test_idle_a', 'text': '我在这儿呢。', 'category': 'idle',
             'cooldown_seconds': 0, 'daily_limit': 20},
        ],
    }, ensure_ascii=False), encoding='utf-8')


class FakeRuntime:
    def __init__(self):
        self.posted = []

    def post(self, request_id, event, payload):
        self.posted.append((request_id, event, payload))


class FakeStore:
    def __init__(self, settings, monitor=None):
        self.settings = settings
        self.ai_key_provider = lambda: 'test-key-not-real'
        self.ai_transport = None
        if monitor is not None:
            self.desktop_monitor = monitor


class FakeMonitor:
    def __init__(self):
        self.snapshots = 0

    def snapshot(self):
        self.snapshots += 1
        return {
            'known': True,
            'fullscreen': True,
            'do_not_disturb': False,
            'locked': False,
            'idle_seconds': 0,
            'sampled_at': NOW.timestamp(),
        }


class NeverCalledProvider:
    def __init__(self):
        self.calls = 0

    def stream_chat(self, messages, key, on_delta=None, cancel_event=None):
        self.calls += 1
        raise AssertionError('the provider must not be called')


def _install_classify(monkeypatch, holder):
    """Install a fake ``service.desktop.monitor`` module for this test only."""
    package = types.ModuleType('service.desktop')
    package.__path__ = []
    module = types.ModuleType('service.desktop.monitor')
    calls = []

    def classify(snapshot, quiet_start='23:00', quiet_end='08:00',
                 idle_after_seconds=300, now=None, last_user_at='',
                 interaction_seconds=120):
        calls.append({'snapshot': snapshot, 'now': now, 'last_user_at': last_user_at,
                      'quiet_start': quiet_start, 'quiet_end': quiet_end,
                      'idle_after_seconds': idle_after_seconds})
        return {'state': holder['state'], 'reason': 'test'}

    module.classify = classify
    monkeypatch.setitem(sys.modules, 'service.desktop', package)
    monkeypatch.setitem(sys.modules, 'service.desktop.monitor', module)
    return calls


def _settings(**autonomy):
    config = {
        'enabled': True,
        'quiet_start': '00:00',
        'quiet_end': '00:00',
        'min_seconds_between': 0,
        'max_per_hour': 5,
        'manual_quiet_until': '',
        'cloud_enabled': False,
    }
    config.update(autonomy)
    return {
        'persona_id': 'godette',
        'ai': {},
        'expression': {'enabled': True, 'phrases_enabled': True},
        'autonomy': config,
        'desktop': _desktop(),
    }


# -- policy_for ------------------------------------------------------------


def test_desktop_off_or_unknown_is_unrestricted():
    for desktop in (None, {}, {'enabled': False, 'work_focus_policy': 'silent'}):
        policy = proactive.policy_for('fullscreen', {}, desktop)
        assert policy == {
            'allowed': True,
            'reason': 'desktop_unknown',
            'categories': [],
            'min_factor': 1.0,
            'max_per_hour': None,
            'state': 'fullscreen',
        }
    unknown = proactive.policy_for('unknown', {}, _desktop(work_focus_policy='silent'))
    assert unknown['allowed'] is True
    assert unknown['reason'] == 'desktop_unknown'
    assert unknown['categories'] == []
    assert unknown['state'] == 'unknown'


@pytest.mark.parametrize('state', ['do_not_disturb', 'fullscreen', 'sleep_hours'])
def test_silent_states_are_never_allowed(state):
    policy = proactive.policy_for(state, {}, _desktop())
    assert policy['allowed'] is False
    assert policy['reason'] == state
    assert policy['categories'] == []
    assert policy['state'] == state
    # A policy value that is neither silent nor minimal stays silent.
    for key, value in (('fullscreen_policy', 'sometimes'),
                       ('dnd_policy', 'normal'),
                       ('work_focus_policy', ''),
                       ('idle_policy', 'off')):
        desktop = _desktop(**{key: value})
        assert proactive.policy_for(state, {}, desktop)['allowed'] is False


def test_fullscreen_and_dnd_minimal_allow_care_only():
    for key, state in (('fullscreen_policy', 'fullscreen'),
                       ('dnd_policy', 'do_not_disturb')):
        policy = proactive.policy_for(state, {}, _desktop(**{key: 'minimal'}))
        assert policy['allowed'] is True
        assert policy['reason'] == 'allowed'
        assert policy['categories'] == ['care']
        assert policy['min_factor'] == 1.0
        assert policy['max_per_hour'] is None


def test_work_focus_policies():
    silent = proactive.policy_for('active_work', {}, _desktop(work_focus_policy='silent'))
    assert silent['allowed'] is False
    assert silent['reason'] == 'active_work'

    minimal = proactive.policy_for(
        'active_work', {}, _desktop(work_focus_policy='minimal'))
    assert minimal['allowed'] is True
    assert minimal['categories'] == ['care']
    assert minimal['min_factor'] == 2.0
    assert minimal['max_per_hour'] == 1

    normal = proactive.policy_for(
        'active_work', {}, _desktop(work_focus_policy='normal'))
    assert normal['allowed'] is True
    assert normal['categories'] == []
    assert normal['min_factor'] == 1.0
    assert normal['max_per_hour'] is None

    unknown = proactive.policy_for(
        'active_work', {}, _desktop(work_focus_policy='sometimes'))
    assert unknown['allowed'] is False


def test_idle_policies():
    gentle = proactive.policy_for('idle', {}, _desktop(idle_policy='gentle'))
    assert gentle['allowed'] is True
    assert gentle['categories'] == ['care', 'idle', 'night']
    assert gentle['min_factor'] == 1.0
    assert gentle['max_per_hour'] is None

    normal = proactive.policy_for('idle', {}, _desktop(idle_policy='normal'))
    assert normal['allowed'] is True
    assert normal['categories'] == []

    silent = proactive.policy_for('idle', {}, _desktop(idle_policy='silent'))
    assert silent['allowed'] is False

    unknown = proactive.policy_for('idle', {}, _desktop(idle_policy='often'))
    assert unknown['allowed'] is False


def test_active_interaction_is_unrestricted():
    policy = proactive.policy_for('active_interaction', {}, _desktop())
    assert policy['allowed'] is True
    assert policy['categories'] == []
    assert policy['min_factor'] == 1.0
    assert policy['max_per_hour'] is None


def test_unrecognized_state_is_treated_as_unknown():
    policy = proactive.policy_for('meeting', {}, _desktop())
    assert policy['allowed'] is True
    assert policy['reason'] == 'desktop_unknown'


# -- budget constraints ----------------------------------------------------


def test_decision_keeps_every_existing_field(database):
    expected = {
        'allowed', 'reason', 'ignored', 'quiet_hours', 'manual_quiet_until',
        'effective_min_seconds', 'effective_max_per_hour', 'hourly_count',
        'last_autonomous_at', 'seconds_until_allowed',
    }
    assert set(budget.can_speak(database, _config(), now=NOW)) == expected
    assert set(budget.can_speak(database, _config(), now=NOW,
                                constraints={'min_factor': 2.0,
                                             'max_per_hour': 1})) == expected


def test_missing_or_invalid_constraints_leave_the_budget_unchanged(database):
    baseline = budget.can_speak(database, _config(), now=NOW)
    assert baseline['allowed'] is True
    assert baseline['effective_min_seconds'] == 600
    assert baseline['effective_max_per_hour'] == 4
    for constraints in (None, {}, {'min_factor': 'x', 'max_per_hour': 'y'},
                        {'min_factor': True, 'max_per_hour': True},
                        {'min_factor': -1, 'max_per_hour': -2},
                        {'min_factor': float('inf')},
                        {'min_factor': float('nan')},
                        {'min_factor': 10 ** 400},
                        # A finite but enormous factor must not overflow int().
                        {'min_factor': 1e308},
                        {'unknown': 1}):
        status = budget.can_speak(database, _config(), now=NOW,
                                  constraints=constraints)
        assert status == baseline


def test_constraints_tighten_the_effective_limits(database):
    config = _config(min_seconds_between=600, max_per_hour=4)
    with database.transaction() as connection:
        budget.record_autonomous(connection, now=NOW)
        # Mark activity so the ignored-message downgrade does not stack here;
        # a separate test covers that stacking.
        budget.note_user_activity(connection, now=NOW + timedelta(seconds=1))

    blocked = budget.can_speak(database, config,
                               now=NOW + timedelta(seconds=700),
                               constraints={'min_factor': 2.0, 'max_per_hour': 4})
    assert blocked['ignored'] is False
    assert blocked['effective_min_seconds'] == 1200
    assert blocked['effective_max_per_hour'] == 4
    assert blocked['reason'] == 'min_interval'
    assert blocked['seconds_until_allowed'] == 500

    capped = budget.can_speak(database, config,
                              now=NOW + timedelta(seconds=700),
                              constraints={'min_factor': 2.0, 'max_per_hour': 1})
    assert capped['effective_min_seconds'] == 1200
    assert capped['effective_max_per_hour'] == 1
    assert capped['reason'] == 'hourly_limit'
    assert capped['hourly_count'] == 1

    allowed = budget.can_speak(database, config,
                               now=NOW + timedelta(seconds=1200),
                               constraints={'min_factor': 2.0, 'max_per_hour': 4})
    assert allowed['allowed'] is True
    assert allowed['effective_min_seconds'] == 1200


def test_constraints_cap_only_lowers_the_hourly_limit(database):
    config = _config(max_per_hour=2)
    status = budget.can_speak(database, config, now=NOW,
                              constraints={'max_per_hour': 5})
    assert status['effective_max_per_hour'] == 2
    assert status['allowed'] is True


def test_constraints_stack_after_the_ignored_downgrade(database):
    config = _config(min_seconds_between=900, max_per_hour=4)
    with database.transaction() as connection:
        budget.record_autonomous(connection, now=NOW)
    status = budget.can_speak(database, config,
                              now=NOW + timedelta(seconds=1200),
                              constraints={'min_factor': 2.0})
    assert status['ignored'] is True
    assert status['effective_min_seconds'] == 3600
    assert status['reason'] == 'min_interval'


def test_constraints_do_not_change_disabled_or_quiet_decisions(database):
    disabled = budget.can_speak(database, _config(enabled=False), now=NOW,
                                constraints={'min_factor': 2.0, 'max_per_hour': 1})
    assert disabled['reason'] == 'disabled'
    quiet = budget.can_speak(database, _config(quiet_start='11:00', quiet_end='13:00'),
                             now=NOW, constraints={'min_factor': 2.0,
                                                   'max_per_hour': 1})
    assert quiet['reason'] == 'quiet_hours'


def test_work_minimal_policy_tightens_the_budget(database):
    desktop = _desktop(work_focus_policy='minimal')
    policy = proactive.policy_for('active_work', {}, desktop)
    config = _config(min_seconds_between=1800, max_per_hour=4)

    first = budget.can_speak(database, config, now=NOW,
                             constraints={'min_factor': policy['min_factor'],
                                          'max_per_hour': policy['max_per_hour']})
    assert first['allowed'] is True
    assert first['effective_min_seconds'] == 3600
    assert first['effective_max_per_hour'] == 1

    with database.transaction() as connection:
        budget.record_autonomous(connection, now=NOW)

    # The hourly cap of one blocks a second message well before the interval.
    capped = budget.can_speak(database, config, now=NOW + timedelta(seconds=60),
                              constraints={'min_factor': policy['min_factor'],
                                           'max_per_hour': policy['max_per_hour']})
    assert capped['reason'] == 'hourly_limit'
    assert capped['hourly_count'] == 1


# -- last_activity ---------------------------------------------------------


def test_last_activity_reads_autonomy_state(database):
    assert budget.last_activity(database) == ''
    assert budget.last_activity(None) == ''
    with database.transaction() as connection:
        budget.note_user_activity(connection, now=NOW)
    assert budget.last_activity(database) == NOW.isoformat()


def test_last_activity_survives_a_broken_connection():
    assert budget.last_activity(object()) == ''


# -- scheduler behaviour ---------------------------------------------------


def test_silent_desktop_state_starts_no_network_request(database, tmp_path,
                                                        monkeypatch):
    _write_phrase_override(tmp_path)
    holder = {'state': 'fullscreen'}
    calls = _install_classify(monkeypatch, holder)
    monitor = FakeMonitor()
    runtime = FakeRuntime()
    provider = NeverCalledProvider()
    store = FakeStore(_settings(), monitor=monitor)
    scheduler = proactive.ProactiveScheduler(
        runtime, store, database,
        provider_factory=lambda ai: provider, interval_seconds=0.0,
        clock=lambda: NOW.timestamp())
    try:
        scheduler.tick(NOW)
        time.sleep(0.02)
        scheduler.tick(NOW)
    finally:
        scheduler.close()
    assert provider.calls == 0
    assert runtime.posted == []
    assert calls and calls[0]['snapshot'] == monitor.snapshot()
    assert database.connection.execute(
        'SELECT COUNT(*) FROM autonomy_state').fetchone()[0] == 0


def test_quiet_hours_come_from_the_autonomy_group(database, tmp_path,
                                                  monkeypatch):
    """The desktop group has no quiet hours; classify must get the autonomy ones."""
    _write_phrase_override(tmp_path)
    holder = {'state': 'active_interaction'}
    calls = _install_classify(monkeypatch, holder)
    settings = _settings(quiet_start='01:30', quiet_end='02:45')
    settings['desktop'] = _desktop(idle_after_seconds=600)
    scheduler = proactive.ProactiveScheduler(
        FakeRuntime(), FakeStore(settings), database, interval_seconds=0.0,
        clock=lambda: NOW.timestamp())
    try:
        scheduler.tick(NOW)
    finally:
        scheduler.close()
    assert calls
    assert calls[0]['quiet_start'] == '01:30'
    assert calls[0]['quiet_end'] == '02:45'
    assert calls[0]['idle_after_seconds'] == 600


def test_disabled_desktop_is_not_sampled(database, tmp_path, monkeypatch):
    _write_phrase_override(tmp_path)
    holder = {'state': 'fullscreen'}
    calls = _install_classify(monkeypatch, holder)
    monitor = FakeMonitor()
    settings = _settings(cloud_enabled=False)
    settings['desktop'] = _desktop(enabled=False)
    store = FakeStore(settings, monitor=monitor)
    runtime = FakeRuntime()
    scheduler = proactive.ProactiveScheduler(
        runtime, store, database, interval_seconds=0.0,
        clock=lambda: NOW.timestamp())
    try:
        scheduler.tick(NOW)
    finally:
        scheduler.close()
    assert monitor.snapshots == 0
    assert calls == []
    assert len(runtime.posted) == 1
    assert runtime.posted[0][2]['source'] == 'local'


def test_work_minimal_doubles_the_scheduler_interval(database, tmp_path,
                                                     monkeypatch):
    _write_phrase_override(tmp_path)
    holder = {'state': 'active_work'}
    _install_classify(monkeypatch, holder)
    settings = _settings(min_seconds_between=1800, max_per_hour=4)
    settings['desktop'] = _desktop(work_focus_policy='minimal')
    runtime = FakeRuntime()
    scheduler = proactive.ProactiveScheduler(
        runtime, FakeStore(settings), database, interval_seconds=0.0,
        clock=lambda: NOW.timestamp())
    try:
        scheduler.tick(NOW)
        assert len(runtime.posted) == 1
        # Mark user activity so the ignored-message downgrade (which also
        # doubles the interval) does not stack on top of the policy factor.
        with database.transaction() as connection:
            budget.note_user_activity(connection, now=NOW + timedelta(seconds=1))

        policy = proactive.policy_for('active_work', {},
                                      settings['desktop'])
        decision = budget.can_speak(
            database, settings['autonomy'], now=NOW + timedelta(seconds=1800),
            constraints={'min_factor': policy['min_factor'],
                         'max_per_hour': policy['max_per_hour']})
        # 1800 s is the plain setting; the policy doubles it to 3600.
        assert decision['effective_min_seconds'] == 3600
        assert decision['effective_max_per_hour'] == 1
        assert decision['reason'] == 'hourly_limit'
        scheduler.tick(NOW + timedelta(seconds=1800))
        assert len(runtime.posted) == 1

        # Past the hourly window and the doubled interval a message is due again.
        scheduler.tick(NOW + timedelta(seconds=3601))
        assert len(runtime.posted) == 2
    finally:
        scheduler.close()


def test_monitor_falls_back_to_the_store(database, tmp_path, monkeypatch):
    _write_phrase_override(tmp_path)
    holder = {'state': 'do_not_disturb'}
    calls = _install_classify(monkeypatch, holder)
    monitor = FakeMonitor()
    store = FakeStore(_settings(), monitor=monitor)
    scheduler = proactive.ProactiveScheduler(
        FakeRuntime(), store, database, interval_seconds=0.0,
        clock=lambda: NOW.timestamp())
    try:
        scheduler.tick(NOW)
    finally:
        scheduler.close()
    assert calls and calls[0]['snapshot'] == monitor.snapshot()


def test_unknown_desktop_module_falls_back_to_unrestricted(database, tmp_path,
                                                           monkeypatch):
    _write_phrase_override(tmp_path)
    # Fail the lazy import so the scheduler must use the 'unknown' fallback.
    monkeypatch.setitem(sys.modules, 'service.desktop.monitor', None)
    settings = _settings()
    settings['desktop'] = _desktop(work_focus_policy='silent')
    runtime = FakeRuntime()
    scheduler = proactive.ProactiveScheduler(
        runtime, FakeStore(settings), database, interval_seconds=0.0,
        clock=lambda: NOW.timestamp())
    try:
        scheduler.tick(NOW)
    finally:
        scheduler.close()
    assert len(runtime.posted) == 1
    assert runtime.posted[0][2]['source'] == 'local'
