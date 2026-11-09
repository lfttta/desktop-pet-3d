"""Personality growth commands and trait locks.

Candidates are generated from stored evidence while ``growth.list`` runs.
Approval, rejection and rollback are explicit commands; every change to a
growable trait is recorded in ``personality_history`` so it can be reverted.
Core traits are always locked and are reported as locked even when a caller
asks to unlock them.
"""

import sqlite3

from service.protocol import ProtocolError
from service.settings import ValidationError
from service.state import character, growth as growth_state

NAME = 'growth'
CAPABILITY = {
    'available': True,
    'reason': '',
    'commands': ['growth.list', 'growth.approve', 'growth.reject', 'growth.history',
                 'growth.rollback', 'personality.lock'],
}

MAX_ID_CHARS = 128


def _database(ctx, request_id):
    database = getattr(ctx, 'database', None)
    if database is None:
        raise ProtocolError('storage_unavailable', 'Growth storage is unavailable.',
                            request_id)
    return database


def _growth_settings(store):
    settings = getattr(store, 'settings', None)
    if isinstance(settings, dict) and isinstance(settings.get('growth'), dict):
        return settings['growth']
    return dict(growth_state.DEFAULT_SETTINGS)


def _identifier(payload, key, request_id):
    value = payload.get(key)
    if type(value) is not str or not value or len(value) > MAX_ID_CHARS:
        raise ProtocolError('invalid_request',
                            f'{key} must be a non-empty identifier.', request_id)
    try:
        value.encode('utf-8')
    except UnicodeError:
        raise ProtocolError('invalid_request', f'{key} is not valid Unicode.',
                            request_id) from None
    return value


def _run(request_id, action):
    try:
        return action()
    except growth_state.GrowthError as exc:
        raise ProtocolError(exc.code, str(exc), request_id) from None
    except sqlite3.Error:
        raise ProtocolError('storage_unavailable', 'Growth storage operation failed.',
                            request_id) from None


def list_growth(request, store, ctx):
    database = _database(ctx, request['id'])
    config = _growth_settings(store)

    def action():
        with database.transaction() as connection:
            growth_state.sync_candidates(connection, config)
            candidates = growth_state.list_candidates(connection)
            entries = growth_state.history(connection)
        return 'result', {'candidates': candidates, 'history': entries}

    return _run(request['id'], action)


def approve(request, store, ctx):
    candidate_id = _identifier(request['payload'], 'candidate_id', request['id'])
    database = _database(ctx, request['id'])
    config = _growth_settings(store)

    def action():
        with database.transaction() as connection:
            outcome = growth_state.approve(connection, candidate_id, config)
        return 'result', outcome

    return _run(request['id'], action)


def reject(request, store, ctx):
    candidate_id = _identifier(request['payload'], 'candidate_id', request['id'])
    database = _database(ctx, request['id'])

    def action():
        with database.transaction() as connection:
            outcome = growth_state.reject(connection, candidate_id)
        return 'result', outcome

    return _run(request['id'], action)


def growth_history(request, store, ctx):
    database = _database(ctx, request['id'])

    def action():
        with database.transaction() as connection:
            entries = growth_state.history(connection)
        return 'result', {'history': entries}

    return _run(request['id'], action)


def rollback(request, store, ctx):
    history_id = _identifier(request['payload'], 'history_id', request['id'])
    database = _database(ctx, request['id'])

    def action():
        with database.transaction() as connection:
            outcome = growth_state.rollback(connection, history_id)
        return 'result', outcome

    return _run(request['id'], action)


def personality_lock(request, store, ctx):
    payload = request['payload']
    trait = payload.get('trait')
    if type(trait) is not str or trait not in character.TRAITS:
        raise ProtocolError('invalid_request',
                            'trait must be one of the character traits.', request['id'])
    locked = payload.get('locked')
    if type(locked) is not bool:
        raise ProtocolError('invalid_request', 'locked must be a boolean.', request['id'])
    # Core traits belong to the character card; they can never be unlocked.
    if trait in character.CORE_TRAITS:
        return 'result', {'trait': trait, 'locked': True}
    current = list(_growth_settings(store).get('locked_traits') or [])
    if locked and trait not in current:
        current.append(trait)
    elif not locked:
        current = [item for item in current if item != trait]
    try:
        store.update({'growth': {'locked_traits': current}})
    except ValidationError as exc:
        raise ProtocolError('invalid_settings', str(exc), request['id']) from None
    except OSError:
        raise ProtocolError('save_failed',
                            'Settings were not saved; last successful values retained.',
                            request['id']) from None
    return 'result', {'trait': trait, 'locked': locked}


COMMANDS = {
    'growth.list': list_growth,
    'growth.approve': approve,
    'growth.reject': reject,
    'growth.history': growth_history,
    'growth.rollback': rollback,
    'personality.lock': personality_lock,
}
