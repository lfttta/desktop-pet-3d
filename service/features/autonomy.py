"""Autonomy budget commands and the local tick suggestion.

``tick`` is called by the runtime heartbeat. It never contacts a provider: when
the budget allows, it picks a local phrase, records the use and returns a
bubble suggestion. The default setting keeps autonomy disabled.
"""

from datetime import datetime, timedelta, timezone
import time

from service.behavior import budget
from service.expression import phrases
from service.protocol import ProtocolError
from service.settings import ValidationError

NAME = 'autonomy'
CAPABILITY = {
    'available': True,
    'reason': '',
    'commands': ['autonomy.get', 'autonomy.update', 'autonomy.set_quiet'],
}
BUBBLE_EVENT = 'autonomy.bubble'
DEFAULT_TICK_INTERVAL = 30.0

MAX_QUIET_MINUTES = 43200


def _database(ctx):
    return getattr(ctx, 'database', None)


def get(request, store, ctx):
    # Imported inside the function: the desktop feature imports this module and
    # a module-level import here would create a cycle.
    from service.features import desktop
    config = store.settings['autonomy']
    status = budget.can_speak(_database(ctx), config)
    return 'result', {'autonomy': config, 'status': status,
                      'desktop': desktop.current(store, ctx)}


def _patch(payload, request_id):
    patch = payload.get('patch', payload)
    if type(patch) is not dict:
        raise ProtocolError('invalid_request', 'autonomy.update requires a patch object.', request_id)
    return patch


def update(request, store, ctx):
    patch = _patch(request['payload'], request['id'])
    try:
        saved = store.update({'autonomy': patch})
    except ValidationError as exc:
        raise ProtocolError('invalid_settings', str(exc), request['id']) from None
    except OSError:
        raise ProtocolError('save_failed', 'Settings were not saved; last successful values retained.',
                            request['id']) from None
    database = _database(ctx)
    if database is not None and 'manual_quiet_until' in patch:
        with database.transaction() as connection:
            budget.set_manual_quiet(connection, budget.parse_timestamp(patch['manual_quiet_until']))
    status = budget.can_speak(database, saved['autonomy'])
    return 'result', {'autonomy': saved['autonomy'], 'status': status}


def set_quiet(request, store, ctx):
    minutes = request['payload'].get('minutes')
    if type(minutes) is not int or not 0 <= minutes <= MAX_QUIET_MINUTES:
        raise ProtocolError('invalid_request',
                            'autonomy.set_quiet requires minutes from 0 to 43200.', request['id'])
    moment = datetime.now().astimezone()
    until = moment + timedelta(minutes=minutes) if minutes else None
    value = until.astimezone(timezone.utc).isoformat() if until is not None else ''
    try:
        saved = store.update({'autonomy': {'manual_quiet_until': value}})
    except ValidationError as exc:
        raise ProtocolError('invalid_settings', str(exc), request['id']) from None
    except OSError:
        raise ProtocolError('save_failed', 'Settings were not saved; last successful values retained.',
                            request['id']) from None
    database = _database(ctx)
    if database is not None:
        with database.transaction() as connection:
            budget.set_manual_quiet(connection, until)
    return 'result', {'autonomy': saved['autonomy'], 'manual_quiet_until': value}


def tick(store, database, now=None, category='idle', rng=None):
    """Return one local bubble suggestion, or None when the budget is closed.

    This function performs no network access. It requires a database so that
    cooldowns, hourly caps and manual quiet state can be enforced.
    """
    if database is None:
        return None
    settings = store.settings
    # Runtime tick hooks pass a monotonic float; fall back to wall-clock time.
    if isinstance(now, (int, float)) and not isinstance(now, bool):
        now = None
    moment = budget.normalize_now(now)
    decision = budget.can_speak(database, settings['autonomy'], now=moment)
    if not decision['allowed']:
        return None
    expression = settings['expression']
    if not expression.get('enabled') or not expression.get('phrases_enabled'):
        return None
    try:
        with database.transaction() as connection:
            phrase = phrases.select_phrase(connection, category=category, now=moment, rng=rng)
            if phrase is None:
                return None
            budget.record_autonomous(connection, now=moment)
    except (OSError, ValueError, UnicodeError):
        return None
    return {
        'text': phrase['text'],
        'phrase_id': phrase['id'],
        'category': phrase['category'],
        'created_at': moment.astimezone(timezone.utc).isoformat(),
    }


def register_tick(runtime, store, database, interval_seconds=DEFAULT_TICK_INTERVAL):
    """Attach the local autonomy tick to a runtime heartbeat."""
    last = {'at': None}

    def hook(now):
        moment = now if isinstance(now, (int, float)) and not isinstance(now, bool) else None
        if moment is None:
            moment = time.monotonic()
        if last['at'] is not None and moment - last['at'] < interval_seconds:
            return
        last['at'] = moment
        suggestion = tick(store, database)
        if suggestion is not None:
            runtime.post('autonomy', BUBBLE_EVENT, suggestion)

    runtime.register_tick(hook)
    return hook


COMMANDS = {
    'autonomy.get': get,
    'autonomy.update': update,
    'autonomy.set_quiet': set_quiet,
}
