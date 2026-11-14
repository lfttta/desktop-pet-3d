"""Feature capabilities advertised in the hello handshake.

A capability is a promise about commands, not about behaviour. Each feature
module owns its own CAPABILITY so this package only aggregates them; all
features start unavailable so the app can render an explicit "not connected"
state instead of pretending to work.
"""

from copy import deepcopy

from service.features import (autonomy, chat, desktop, diag, expression, growth, memory,
                              state, timers, usage, voice)

FEATURE_MODULES = (autonomy, chat, memory, state, expression, growth, timers, usage, diag,
                   desktop, voice)


def capabilities():
    return deepcopy({module.NAME: module.CAPABILITY for module in FEATURE_MODULES})
