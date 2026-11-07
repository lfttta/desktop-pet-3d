"""Desktop attention status command.

The command exposes the current attention sample and the speaking policy the
scheduler derives from it. The monitor is created lazily and cached on the
store so repeated polls share one debounced sampler. Nothing here writes
storage or performs network access, and every failure degrades to ``unknown``
so an app can render a stable "not connected" state.
"""

from service.behavior import budget, proactive
from service.desktop.monitor import DesktopMonitor, classify

NAME = 'desktop'
CAPABILITY = {
    'available': True,
    'reason': '',
    'commands': ['desktop.status'],
}

# Mirrors ``service.desktop.monitor.SNAPSHOT_KEY_ORDER``; the fixed shape is
# what the app renders when no reading is available.
UNKNOWN_SNAPSHOT = {
    'known': False,
    'fullscreen': False,
    'do_not_disturb': False,
    'locked': False,
    'idle_seconds': 0,
    'sampled_at': 0.0,
}


def _settings(store):
    settings = getattr(store, 'settings', None)
    return settings if isinstance(settings, dict) else {}


def _group(settings, name):
    value = settings.get(name)
    return value if isinstance(value, dict) else {}


def _monitor(store, config):
    """Return the cached monitor, creating it only for an enabled feature."""
    monitor = getattr(store, 'desktop_monitor', None)
    if monitor is None:
        monitor = DesktopMonitor(poll_seconds=config.get('poll_seconds', 3))
        store.desktop_monitor = monitor
    return monitor


def _unknown(enabled=False, reason='unknown'):
    """Return a fixed-shape status document.

    ``enabled`` mirrors the user's switch, so a transient sampling failure
    (enabled but unreadable) is not reported as "desktop sensing is off".
    """
    policy = proactive.policy_for('unknown', {}, {})
    return {
        'enabled': bool(enabled),
        'snapshot': dict(UNKNOWN_SNAPSHOT),
        'state': 'unknown',
        'reason': reason,
        'policy': policy,
    }


def current(store, ctx):
    """Return one desktop status document; never raises."""
    try:
        settings = _settings(store)
        desktop = _group(settings, 'desktop')
        enabled = desktop.get('enabled') is True
        if not enabled:
            # Turning the switch off also releases the cached sampler so the
            # backend stops reading the desktop for the rest of the session.
            proactive.release_store_monitor(store)
            return _unknown(False, 'unknown')
        autonomy = _group(settings, 'autonomy')
        snapshot = _monitor(store, desktop).snapshot()
        if not isinstance(snapshot, dict):
            snapshot = dict(UNKNOWN_SNAPSHOT)
        if snapshot.get('known') is not True:
            # The Win32 probe could not deliver a reading; sensing itself is
            # still on, so this must not look like a user setting change.
            return _unknown(True, 'sampling_failed')
        # Same option set the scheduler passes, so the displayed state always
        # matches the state the speaking policy is derived from. Quiet hours
        # come from the autonomy group; the desktop group has no such keys.
        options = {key: autonomy[key] for key in proactive.AUTONOMY_CLASSIFY_KEYS
                   if key in autonomy}
        options.update({key: desktop[key] for key in proactive.DESKTOP_CLASSIFY_KEYS
                        if key in desktop})
        last_user_at = budget.last_activity(getattr(ctx, 'database', None))
        if last_user_at:
            options['last_user_at'] = last_user_at
        result = classify(snapshot, **options)
        state = result.get('state') if isinstance(result, dict) else 'unknown'
        if type(state) is not str:
            state = 'unknown'
        reason = result.get('reason') if isinstance(result, dict) else ''
        if type(reason) is not str:
            reason = ''
        return {
            'enabled': True,
            'snapshot': snapshot,
            'state': state,
            'reason': reason,
            'policy': proactive.policy_for(state, autonomy, desktop),
        }
    except Exception:
        # The switch state was unreadable, so do not claim the user turned it
        # off; report an unavailable sample instead.
        return _unknown(True, 'sampling_failed')


def status(request, store, ctx):
    return 'result', current(store, ctx)


COMMANDS = {'desktop.status': status}
