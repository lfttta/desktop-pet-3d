"""Persona catalog and prompt boundary for the chat framework.

Prompts stay empty until chat generation is implemented. The catalog is the
single source for valid persona ids and for the public list sent to the app;
private prompt text is never exposed over the protocol.
"""

PERSONAS = {
    'godette': {
        'id': 'godette',
        'display_name': 'Godette',
        'prompt': '',
    },
}
DEFAULT_PERSONA_ID = 'godette'
ALLOWED_PERSONA_IDS = frozenset(PERSONAS)


def public_catalog():
    """Return the persona fields the app may display."""
    return [{'id': item['id'], 'display_name': item['display_name']}
            for item in PERSONAS.values()]


def resolve(persona_id):
    """Return the full catalog entry, including the prompt, or None."""
    return PERSONAS.get(persona_id)
