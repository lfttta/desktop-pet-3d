"""Durable PAD character state with smoothing, decay and locked personality.

The model may only propose deltas for the PAD axes and the small set of
psychological variables. This module owns clamping, smoothing and time decay.
It always rebuilds the server-side personality configuration and restores core
traits from the character card, so a model response can never write personality
values; only an approved growth candidate may move a growable trait.
"""

from copy import deepcopy
from datetime import datetime, timezone
import math

from service.database import SUBJECT_USER_ID
from service.jsonutil import dumps, loads

PAD_KEYS = ('pleasure', 'arousal', 'dominance')
PSYCH_KEYS = ('energy', 'affinity', 'stress')
PAD_BOUNDS = {key: (-1.0, 1.0) for key in PAD_KEYS}
PSYCH_BOUNDS = {key: (0.0, 1.0) for key in PSYCH_KEYS}
PSYCH_BASELINE = {'energy': 0.6, 'affinity': 0.45, 'stress': 0.1}

# Trait values live in [0, 1]. Core traits are fixed by the character card and
# can never change; growable traits may only be moved by an approved growth
# candidate, never by a model delta.
CORE_TRAITS = ('kindness', 'independence')
GROWABLE_TRAITS = ('warmth', 'curiosity', 'playfulness', 'directness')
TRAITS = CORE_TRAITS + GROWABLE_TRAITS
TRAIT_BOUNDS = {trait: (0.0, 1.0) for trait in TRAITS}

# Built-in character card. Personality defaults come from this card only; a
# model response can supply and change none of them.
CHARACTER_CARD = {
    'id': 'godette',
    'personality': {
        'kindness': 0.78,
        'independence': 0.62,
        'warmth': 0.55,
        'curiosity': 0.5,
        'playfulness': 0.5,
        'directness': 0.5,
    },
}

# Code-owned personality configuration. The trait defaults mirror the character
# card; only approved growth may replace the stored values of growable traits.
PERSONALITY = {
    'baseline': {'pleasure': 0.08, 'arousal': -0.05, 'dominance': 0.02},
    'decay_seconds': 3600.0,
    'smoothing': 0.5,
    'expressiveness': 0.6,
    'warmth_bias': 0.55,
    'traits': deepcopy(CHARACTER_CARD['personality']),
    # Trait values are mirrored at the top level for direct inspection.
    **deepcopy(CHARACTER_CARD['personality']),
}


def normalize_now(value=None):
    """Return an aware datetime; naive input is interpreted as UTC."""
    if value is None:
        return datetime.now(timezone.utc)
    if not isinstance(value, datetime):
        raise TypeError('now must be a datetime')
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso(value):
    return normalize_now(value).isoformat()


def _parse_timestamp(value):
    if type(value) is not str or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def clamp(value, low, high):
    return max(low, min(high, float(value)))


def _clamp_group(values, bounds, fallback):
    result = {}
    for key, (low, high) in bounds.items():
        candidate = values.get(key) if isinstance(values, dict) else None
        if not _number(candidate):
            candidate = fallback[key]
        result[key] = clamp(candidate, low, high)
    return result


def default_state(now=None):
    """Return a fresh state document at the personality baseline."""
    return {
        'pad': {key: float(value) for key, value in PERSONALITY['baseline'].items()},
        'psych': {key: float(value) for key, value in PSYCH_BASELINE.items()},
        'personality': _normalize_personality(None),
        'updated_at': _iso(now),
    }


def _normalize_personality(document):
    """Return the code-owned personality block with persisted growable traits.

    The configuration section is always rebuilt from ``PERSONALITY``; only the
    values of growable traits survive a reload. Core traits always snap back to
    the character card and any unknown or malformed stored value is ignored.
    """
    stored = document.get('personality') if isinstance(document, dict) else None
    stored_traits = stored.get('traits') if isinstance(stored, dict) else None
    if not isinstance(stored_traits, dict):
        # Accept a flat personality block written by older builds.
        stored_traits = stored
    traits = {}
    for trait in TRAITS:
        candidate = (stored_traits.get(trait)
                     if isinstance(stored_traits, dict) else None)
        if trait in CORE_TRAITS or not _number(candidate):
            candidate = CHARACTER_CARD['personality'][trait]
        low, high = TRAIT_BOUNDS[trait]
        traits[trait] = clamp(candidate, low, high)
    normalized = deepcopy(PERSONALITY)
    normalized['traits'] = traits
    for trait in TRAITS:
        normalized[trait] = traits[trait]
    return normalized


def personality_values(state):
    """Return a copy of the six trait values held by a state document."""
    personality = state.get('personality') if isinstance(state, dict) else None
    traits = personality.get('traits') if isinstance(personality, dict) else None
    if not isinstance(traits, dict):
        traits = personality
    result = {}
    for trait in TRAITS:
        value = traits.get(trait) if isinstance(traits, dict) else None
        if not _number(value):
            value = CHARACTER_CARD['personality'][trait]
        low, high = TRAIT_BOUNDS[trait]
        result[trait] = clamp(value, low, high)
    return result


def locked_traits(growth_settings=None):
    """Return core traits plus traits locked through ``growth.locked_traits``.

    ``growth_settings`` is the ``growth`` settings group, not the full settings
    document. Unknown trait names are ignored because only card traits exist.
    """
    locked = set(CORE_TRAITS)
    configured = (growth_settings.get('locked_traits')
                  if isinstance(growth_settings, dict) else None)
    if isinstance(configured, (list, tuple)):
        for trait in configured:
            if type(trait) is str and trait in GROWABLE_TRAITS:
                locked.add(trait)
    return [trait for trait in TRAITS if trait in locked]


def is_growable(trait):
    return trait in GROWABLE_TRAITS


def _normalize_state(document, now):
    if type(document) is not dict:
        return default_state(now)
    state = {
        'pad': _clamp_group(document.get('pad'), PAD_BOUNDS, PERSONALITY['baseline']),
        'psych': _clamp_group(document.get('psych'), PSYCH_BOUNDS, PSYCH_BASELINE),
        'personality': _normalize_personality(document),
        'updated_at': _iso(now),
    }
    prior = _parse_timestamp(document.get('updated_at'))
    if prior is not None:
        state['updated_at'] = prior.isoformat()
    return state


def _save(connection, state):
    connection.execute(
        'INSERT INTO character_state(subject_user_id, payload, updated_at) VALUES (?, ?, ?) '
        'ON CONFLICT(subject_user_id) DO UPDATE SET '
        'payload=excluded.payload, updated_at=excluded.updated_at',
        (SUBJECT_USER_ID, dumps(state).decode('utf-8'), state['updated_at']))


def load_state(connection, now=None, apply_decay=True):
    """Read and normalize state, optionally applying time decay and saving it."""
    moment = normalize_now(now)
    if connection is None:
        return default_state(moment)
    row = connection.execute(
        'SELECT payload FROM character_state WHERE subject_user_id=?',
        (SUBJECT_USER_ID,)).fetchone()
    if row is None:
        state = default_state(moment)
        _save(connection, state)
        return state
    try:
        document = loads(row[0].encode('utf-8')) if type(row[0]) is str else {}
    except (ValueError, UnicodeError, RecursionError):
        document = {}
    state = _normalize_state(document, moment)
    if apply_decay:
        state = _decay_state(state, moment)
    _save(connection, state)
    return state


def _decay_state(state, moment):
    """Move every variable toward its baseline with elapsed-time exponential decay."""
    updated = _parse_timestamp(state.get('updated_at'))
    personality = state['personality']
    if updated is not None:
        elapsed = (moment - updated).total_seconds()
        if elapsed > 0:
            rate = max(1.0, float(personality['decay_seconds']))
            factor = math.exp(-elapsed / rate)
            for key, baseline in personality['baseline'].items():
                value = state['pad'][key]
                state['pad'][key] = clamp(baseline + (value - baseline) * factor, -1.0, 1.0)
            for key, baseline in PSYCH_BASELINE.items():
                value = state['psych'][key]
                state['psych'][key] = clamp(baseline + (value - baseline) * factor, 0.0, 1.0)
    state['updated_at'] = moment.isoformat()
    return state


def decay(connection, now=None):
    """Apply time decay and persist the result."""
    moment = normalize_now(now)
    state = load_state(connection, now=moment, apply_decay=False)
    state = _decay_state(state, moment)
    if connection is not None:
        _save(connection, state)
    return state


def apply_delta(connection, delta, now=None):
    """Apply a model-proposed delta with smoothing and server-side clamping.

    ``delta`` may be structured as ``{'pad': {...}, 'psych': {...}}`` or carry
    the individual keys at the top level. Unknown, non-numeric and personality
    keys are ignored; the model cannot set absolute values or override the
    locked personality block.
    """
    if type(delta) is not dict:
        raise ValueError('delta must be a mapping')
    moment = normalize_now(now)
    state = load_state(connection, now=moment, apply_decay=True)
    smoothing = float(state['personality']['smoothing'])

    pad_delta = delta.get('pad') if isinstance(delta.get('pad'), dict) else delta
    psych_delta = delta.get('psych') if isinstance(delta.get('psych'), dict) else delta

    for key, (low, high) in PAD_BOUNDS.items():
        amount = pad_delta.get(key)
        if _number(amount):
            state['pad'][key] = clamp(state['pad'][key] + float(amount) * smoothing, low, high)
    for key, (low, high) in PSYCH_BOUNDS.items():
        amount = psych_delta.get(key)
        if _number(amount):
            state['psych'][key] = clamp(state['psych'][key] + float(amount) * smoothing, low, high)

    # Personality is code-owned. The model cannot supply trait values or the
    # configuration block, but growable values already persisted by an approved
    # growth candidate are preserved by load_state and written back unchanged.
    state['personality'] = _normalize_personality(state)
    state['updated_at'] = moment.isoformat()
    if connection is not None:
        _save(connection, state)
    return state


def apply_trait_delta(connection, trait, delta, max_step, now=None):
    """Move one growable trait by at most ``max_step`` and persist it.

    Only the server-side growth pipeline calls this function after a candidate
    has been approved. Core traits and unknown traits are rejected, so a buggy
    caller can never rewrite the character card.
    """
    if trait not in GROWABLE_TRAITS:
        raise ValueError('trait is not growable')
    moment = normalize_now(now)
    state = load_state(connection, now=moment, apply_decay=False)
    low, high = TRAIT_BOUNDS[trait]
    old_value = float(state['personality']['traits'][trait])
    limit = abs(float(max_step))
    step = clamp(delta, -limit, limit)
    new_value = round(clamp(old_value + step, low, high), 6)
    state['personality']['traits'][trait] = new_value
    state['updated_at'] = moment.isoformat()
    if connection is not None:
        _save(connection, state)
    return old_value, new_value


def restore_trait(connection, trait, value, now=None):
    """Restore one growable trait to an explicit value (growth rollback)."""
    if trait not in GROWABLE_TRAITS:
        raise ValueError('trait is not growable')
    moment = normalize_now(now)
    state = load_state(connection, now=moment, apply_decay=False)
    low, high = TRAIT_BOUNDS[trait]
    restored = round(clamp(value, low, high), 6)
    state['personality']['traits'][trait] = restored
    state['updated_at'] = moment.isoformat()
    if connection is not None:
        _save(connection, state)
    return restored


def reset_state(connection, now=None):
    """Restore the baseline state and persist it."""
    moment = normalize_now(now)
    state = default_state(moment)
    if connection is not None:
        _save(connection, state)
    return state


# Presentation mapping. The PAD pleasure axis selects the base expression; the
# psych axes refine it into a richer emotion label without requiring new blend
# shapes. Every label still renders through one of the three existing
# expression channels (neutral / happy / sad). Thresholds are inclusive at the
# boundary.
HAPPY_THRESHOLD = 0.20
SAD_THRESHOLD = -0.15
EXCITED_AROUSAL = 0.35
TIRED_ENERGY = 0.25
UPSET_STRESS = 0.50
MIN_EXPRESSION_WEIGHT = 0.35

# Rich emotion labels the service may send, and the existing expression each
# one renders through. This is the only copy the service owns; the app keeps a
# matching table so both sides can fall back without a round trip.
EXPRESSION_BY_EMOTION = {
    'neutral': 'neutral',
    'happy': 'happy',
    'sad': 'sad',
    'excited': 'happy',
    'tired': 'sad',
    'upset': 'sad',
}


def expression_for(emotion):
    """Return the existing expression channel an emotion label renders through.

    Unknown, missing and non-string labels fall back to ``neutral``: it is the
    one face a model without new blend shapes can always show. This function
    never raises.
    """
    if type(emotion) is not str:
        return 'neutral'
    return EXPRESSION_BY_EMOTION.get(emotion, 'neutral')


def _presentation_axis(state, group, key, fallback):
    """Return a finite numeric axis value, or ``fallback`` when unusable."""
    if not isinstance(state, dict):
        return fallback
    values = state.get(group)
    if not isinstance(values, dict):
        return fallback
    value = values.get(key)
    # ``bool`` is an ``int`` subclass but not a meaningful PAD/psych value.
    if type(value) not in (int, float):
        return fallback
    try:
        # Huge integers make math.isfinite raise OverflowError.
        if not math.isfinite(value):
            return fallback
        return float(value)
    except (OverflowError, TypeError, ValueError):
        return fallback


def _presentation_pleasure(state):
    """Return the finite pleasure value of a state document, else ``None``."""
    return _presentation_axis(state, 'pad', 'pleasure', None)


def _expression_weight(magnitude):
    """Blend weight for a non-negative pleasure magnitude, rounded to 4 places."""
    weight = clamp(0.45 + magnitude * 0.55, MIN_EXPRESSION_WEIGHT, 1.0)
    return round(weight, 4)


def _emotion_label(pleasure, arousal, energy, stress):
    """Pick the rich emotion label for one PAD/psych sample.

    ``happy``/``sad`` stay the pleasure-only labels so callers that only know
    the PAD axes keep working. The three richer labels need a supporting axis:

    * ``excited`` — pleasant and aroused (pleasure decides, so a happy pet stays
      happy even when it is tired)
    * ``upset`` — stressed and sad
    * ``tired`` — low energy, whether the pleasure is sad or neutral

    Inside the sad band ``upset`` outranks ``tired`` because a stressed and
    exhausted pet reads as upset first. Inside the neutral band ``upset`` needs
    non-positive pleasure, so mild stress with a positive reading cannot flip
    the face.
    """
    if pleasure >= HAPPY_THRESHOLD:
        return 'excited' if arousal >= EXCITED_AROUSAL else 'happy'
    if pleasure <= SAD_THRESHOLD:
        if stress >= UPSET_STRESS:
            return 'upset'
        return 'tired' if energy <= TIRED_ENERGY else 'sad'
    if stress >= UPSET_STRESS and pleasure <= 0.0:
        return 'upset'
    if energy <= TIRED_ENERGY:
        return 'tired'
    return 'neutral'


def presentation(state):
    """Map a state document onto ``{'emotion': label, 'weight': float}``.

    ``emotion`` is one of ``neutral``, ``happy``, ``sad`` (the original
    pleasure-only labels) or the richer ``excited``, ``tired`` and ``upset``.
    ``excited`` needs pleasure at ``HAPPY_THRESHOLD`` with arousal at
    ``EXCITED_AROUSAL``; ``upset`` needs stress at ``UPSET_STRESS`` with
    non-positive pleasure; ``tired`` needs energy at ``TIRED_ENERGY`` or below.
    The weight grows with the absolute pleasure value and is always clamped to
    ``[MIN_EXPRESSION_WEIGHT, 1.0]``. Missing, malformed or non-finite input
    yields the neutral presentation; this function never raises.

    Every label renders through an existing expression; ``expression_for`` is
    the single source for that mapping, so no new blend shape is required.
    """
    try:
        pleasure = _presentation_pleasure(state)
    except Exception:
        # A hostile mapping may raise from ``get``; presentation is best-effort.
        pleasure = None
    if pleasure is None:
        return {'emotion': 'neutral', 'weight': 1.0}
    try:
        arousal = _presentation_axis(state, 'pad', 'arousal', 0.0)
        energy = _presentation_axis(state, 'psych', 'energy', PSYCH_BASELINE['energy'])
        stress = _presentation_axis(state, 'psych', 'stress', PSYCH_BASELINE['stress'])
    except Exception:
        arousal = 0.0
        energy = PSYCH_BASELINE['energy']
        stress = PSYCH_BASELINE['stress']
    emotion = _emotion_label(pleasure, arousal, energy, stress)
    if emotion == 'neutral':
        return {'emotion': 'neutral', 'weight': 1.0}
    return {'emotion': emotion, 'weight': _expression_weight(abs(pleasure))}
