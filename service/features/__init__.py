"""Feature capabilities advertised in the hello handshake.

A capability is a promise about commands, not about behaviour. Chat currently
publishes its contract with available=False so the app can render an explicit
"not connected" state instead of pretending to work.
"""

from copy import deepcopy

FEATURES = {
    'chat': {
        'available': False,
        'reason': 'not_implemented',
        'commands': ['chat.send', 'chat.cancel'],
    },
}


def capabilities():
    return deepcopy(FEATURES)
