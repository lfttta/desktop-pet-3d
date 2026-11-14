"""Emotion presentation mapping contracts for the PAD character state."""

import pytest

from service.state import character


def _state(pleasure):
    return {'pad': {'pleasure': pleasure, 'arousal': 0.0, 'dominance': 0.0}}


def test_constants_match_presentation_contract():
    assert character.HAPPY_THRESHOLD == 0.20
    assert character.SAD_THRESHOLD == -0.15
    assert character.MIN_EXPRESSION_WEIGHT == 0.35


@pytest.mark.parametrize('pleasure, emotion', [
    (0.20, 'happy'),          # exactly at the happy threshold
    (-0.15, 'sad'),           # exactly at the sad threshold
    (0.19, 'neutral'),        # just below the happy threshold
    (-0.14, 'neutral'),       # just above the sad threshold
    (0.0, 'neutral'),
    (0.5, 'happy'),
    (-0.5, 'sad'),
    (1.0, 'happy'),
    (-1.0, 'sad'),
])
def test_threshold_boundaries_and_emotion_selection(pleasure, emotion):
    result = character.presentation(_state(pleasure))
    assert result['emotion'] == emotion
    assert set(result) == {'emotion', 'weight'}
    assert type(result['weight']) is float


def test_weight_at_boundaries_is_clamped_and_rounded():
    assert character.presentation(_state(0.20))['weight'] == 0.56
    assert character.presentation(_state(-0.15))['weight'] == 0.5325
    # Full saturation caps at 1.0 on both sides.
    assert character.presentation(_state(1.0))['weight'] == 1.0
    assert character.presentation(_state(-1.0))['weight'] == 1.0
    # Neutral always presents at full strength.
    assert character.presentation(_state(0.0))['weight'] == 1.0


def test_weight_grows_with_pleasure_magnitude():
    happy_low = character.presentation(_state(0.20))['weight']
    happy_high = character.presentation(_state(0.60))['weight']
    sad_low = character.presentation(_state(-0.15))['weight']
    sad_high = character.presentation(_state(-0.60))['weight']
    assert happy_low < happy_high
    assert sad_low < sad_high
    # Symmetric magnitudes produce the same weight.
    assert happy_high == sad_high


def test_weight_stays_inside_range_and_has_four_decimals():
    for step in range(-100, 101):
        pleasure = step / 100
        weight = character.presentation(_state(pleasure))['weight']
        assert character.MIN_EXPRESSION_WEIGHT <= weight <= 1.0
        assert round(weight, 4) == weight
    # A value that would produce more than four decimals is rounded.
    weight = character.presentation(_state(0.3333))['weight']
    assert weight == round(0.45 + 0.3333 * 0.55, 4)
    assert round(weight, 4) == weight


@pytest.mark.parametrize('bad_state', [
    None,
    {},
    [],
    'state',
    42,
    3.5,
    True,
    {'pad': None},
    {'pad': []},
    {'pad': 'pleasure'},
    {'pad': {}},
    {'pad': {'pleasure': None}},
    {'pad': {'pleasure': 'happy'}},
    {'pad': {'pleasure': True}},
    {'pad': {'pleasure': float('nan')}},
    {'pad': {'pleasure': float('inf')}},
    {'pad': {'pleasure': float('-inf')}},
    {'pad': {'pleasure': 10 ** 400}},   # overflows float conversion
    {'pad': {'arousal': 1.0}},
    {'pleasure': 1.0},
])
def test_invalid_input_falls_back_to_neutral_without_raising(bad_state):
    assert character.presentation(bad_state) == {'emotion': 'neutral', 'weight': 1.0}


def test_mapping_that_raises_on_access_still_returns_neutral():
    class Hostile(dict):
        def get(self, *args, **kwargs):
            raise RuntimeError('boom')

    assert character.presentation(Hostile()) == {'emotion': 'neutral', 'weight': 1.0}


def test_neutral_ignores_other_axes_and_does_not_mutate_state():
    state = {'pad': {'pleasure': 0.0, 'arousal': 1.0, 'dominance': -1.0}}
    before = {key: dict(value) if isinstance(value, dict) else value
              for key, value in state.items()}
    assert character.presentation(state) == {'emotion': 'neutral', 'weight': 1.0}
    assert state == before


def test_default_state_is_neutral_at_baseline():
    state = character.default_state()
    assert state['pad']['pleasure'] < character.HAPPY_THRESHOLD
    assert character.presentation(state) == {'emotion': 'neutral', 'weight': 1.0}


def test_integer_pleasure_is_accepted():
    assert character.presentation(_state(1)) == {'emotion': 'happy', 'weight': 1.0}
    assert character.presentation(_state(0)) == {'emotion': 'neutral', 'weight': 1.0}
    assert character.presentation(_state(-1)) == {'emotion': 'sad', 'weight': 1.0}


# --- Rich labels -------------------------------------------------------------
#
# The three extra labels reuse the existing faces. Their thresholds are service
# constants so the app can mirror them without a round trip; the boundary values
# below are inclusive, matching the happy/sad thresholds.

def _rich_state(pleasure=0.0, arousal=0.0, energy=0.6, stress=0.1):
    return {
        'pad': {'pleasure': pleasure, 'arousal': arousal, 'dominance': 0.0},
        'psych': {'energy': energy, 'affinity': 0.45, 'stress': stress},
    }


@pytest.mark.parametrize('state, emotion', [
    # excited: pleasant and aroused, at the arousal threshold exactly.
    (_rich_state(0.20, character.EXCITED_AROUSAL), 'excited'),
    (_rich_state(1.0, 1.0), 'excited'),
    # happy wins because arousal is just below the excited threshold.
    (_rich_state(0.20, character.EXCITED_AROUSAL - 0.01), 'happy'),
    # tired: low energy, at the energy threshold exactly.
    (_rich_state(0.0, 0.0, character.TIRED_ENERGY), 'tired'),
    (_rich_state(-0.5, 0.0, 0.0), 'tired'),
    # A happy pet stays happy even when its energy is low; pleasure decides.
    (_rich_state(0.5, 0.0, 0.0), 'happy'),
    # upset: stressed and at/below the sad threshold.
    (_rich_state(-0.15, 0.0, 0.6, character.UPSET_STRESS), 'upset'),
    (_rich_state(-0.8, 0.0, 0.6, 1.0), 'upset'),
    # Stress just below the threshold leaves the plain sad label.
    (_rich_state(-0.5, 0.0, 0.6, character.UPSET_STRESS - 0.01), 'sad'),
    # Upset outranks tired inside the sad band.
    (_rich_state(-0.5, 0.0, 0.0, 1.0), 'upset'),
    # Mild stress without negative pleasure must not flip the face.
    (_rich_state(0.1, 0.0, 0.6, 0.9), 'neutral'),
    # Stress at the threshold with a non-positive reading is upset even inside
    # the neutral band; at exactly 0.0 it counts as non-positive.
    (_rich_state(0.0, 0.0, 0.6, 0.9), 'upset'),
    (_rich_state(-0.01, 0.0, 0.6, character.UPSET_STRESS), 'upset'),
])
def test_rich_labels_use_the_pad_and_psych_axes(state, emotion):
    result = character.presentation(state)
    assert result['emotion'] == emotion
    assert set(result) == {'emotion', 'weight'}
    assert type(result['weight']) is float
    assert character.MIN_EXPRESSION_WEIGHT <= result['weight'] <= 1.0


def test_missing_psych_axes_keep_the_pleasure_only_labels():
    # A document with only PAD must not accidentally become tired or upset: the
    # psych fallbacks are the baseline, not zero.
    assert character.presentation(_state(0.5))['emotion'] == 'happy'
    assert character.presentation(_state(-0.5))['emotion'] == 'sad'
    assert character.presentation(_state(0.0))['emotion'] == 'neutral'
    # Malformed psych values fall back to the same baseline.
    malformed = _rich_state(0.0)
    malformed['psych'] = {'energy': 'low', 'stress': None}
    assert character.presentation(malformed)['emotion'] == 'neutral'


def test_rich_labels_preserve_the_weight_contract():
    happy = character.presentation(_rich_state(0.5, 0.0))['weight']
    excited = character.presentation(_rich_state(0.5, 1.0))['weight']
    sad = character.presentation(_rich_state(-0.5))['weight']
    tired = character.presentation(_rich_state(-0.5, 0.0, 0.0))['weight']
    upset = character.presentation(_rich_state(-0.5, 0.0, 0.6, 1.0))['weight']
    # The weight still follows |pleasure| for every label, so a rich label is
    # never weaker than the plain label it replaces.
    assert happy == excited == sad == tired == upset
    assert happy == character.presentation(_state(0.5))['weight']


# --- Expression mapping ------------------------------------------------------

def test_expression_mapping_reuses_existing_faces_only():
    assert character.EXPRESSION_BY_EMOTION['neutral'] == 'neutral'
    assert character.EXPRESSION_BY_EMOTION['happy'] == 'happy'
    assert character.EXPRESSION_BY_EMOTION['sad'] == 'sad'
    assert character.EXPRESSION_BY_EMOTION['excited'] == 'happy'
    assert character.EXPRESSION_BY_EMOTION['tired'] == 'sad'
    assert character.EXPRESSION_BY_EMOTION['upset'] == 'sad'
    # Every value is one of the three blend shapes the model already ships.
    assert set(character.EXPRESSION_BY_EMOTION.values()) <= {'neutral', 'happy', 'sad'}


@pytest.mark.parametrize('emotion, expression', [
    ('neutral', 'neutral'),
    ('happy', 'happy'),
    ('sad', 'sad'),
    ('excited', 'happy'),
    ('tired', 'sad'),
    ('upset', 'sad'),
])
def test_expression_for_known_labels(emotion, expression):
    assert character.expression_for(emotion) == expression


@pytest.mark.parametrize('unknown', [None, '', 'angry', 'HAPPY', 3, 3.5, True, [], {}])
def test_expression_for_unknown_labels_falls_back_to_neutral(unknown):
    assert character.expression_for(unknown) == 'neutral'


def test_presentation_labels_are_all_renderable():
    outcomes = set()
    for step in range(-100, 101):
        pleasure = step / 100
        for arousal in (0.0, 1.0):
            for energy in (0.0, 0.6):
                for stress in (0.0, 1.0):
                    label = character.presentation(
                        _rich_state(pleasure, arousal, energy, stress))['emotion']
                    assert label in character.EXPRESSION_BY_EMOTION
                    assert character.expression_for(label) in ('neutral', 'happy', 'sad')
                    outcomes.add(label)
    # The sweep really exercises every label; a threshold typo would drop one.
    assert outcomes == set(character.EXPRESSION_BY_EMOTION)


def test_presentation_does_not_raise_on_hostile_psych_values():
    class Hostile(dict):
        def get(self, *args, **kwargs):
            raise RuntimeError('boom')

    state = {'pad': {'pleasure': 0.5}, 'psych': Hostile()}
    assert character.presentation(state) == {'emotion': 'happy', 'weight': 0.725}
