"""Character state commands backed by ``character_state``."""

from service.state import character

NAME = 'state'
CAPABILITY = {
    'available': True,
    'reason': '',
    'commands': ['state.get', 'state.reset'],
}


def _database(ctx):
    return getattr(ctx, 'database', None)


def _growth_settings(store):
    settings = getattr(store, 'settings', None)
    if isinstance(settings, dict) and isinstance(settings.get('growth'), dict):
        return settings['growth']
    return None


def _view(state, store):
    """Return state plus the trait values and locked list the app displays."""
    return {
        'state': state,
        'personality': character.personality_values(state),
        'locked': character.locked_traits(_growth_settings(store)),
    }


def get(request, store, ctx):
    database = _database(ctx)
    if database is None:
        return 'result', _view(character.default_state(), store)
    with database.transaction() as connection:
        state = character.decay(connection)
    return 'result', _view(state, store)


def reset(request, store, ctx):
    database = _database(ctx)
    if database is None:
        return 'result', _view(character.default_state(), store)
    with database.transaction() as connection:
        state = character.reset_state(connection)
    return 'result', _view(state, store)


COMMANDS = {'state.get': get, 'state.reset': reset}
