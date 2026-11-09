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
