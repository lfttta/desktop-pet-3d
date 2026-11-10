"""The chat action vocabulary must stay in step with the shipped clip library.

The assistant can only name animations that exist and that the app can play from
a reply. Keeping the list hand-written next to the manifest is deliberate — the
manifest also carries loop and hold clips the chat path must not drive — so this
test is what stops the two from drifting apart.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = json.loads((ROOT / 'animations/humanoid_core/manifest.json').read_text('utf-8'))
ACTIONS = MANIFEST['actions']

sys.path.insert(0, str(ROOT))
from service.ai.actions import CHAT_ACTIONS, NO_ACTION, SPECIAL_ACTIONS, allowed, prompt_list  # noqa: E402

# Clips the chat path deliberately never drives, with the reason each one is out.
EXCLUDED = {
    'pickup': 'hold', 'fall': 'hold',
    'turn': 'rotation', 'recover': 'landing path',
    'male_sitting_pose': 'degenerate frame',
}


def test_every_chat_action_exists_in_the_library():
    missing = sorted(CHAT_ACTIONS - set(ACTIONS))
    assert not missing, f'chat vocabulary names unknown clips: {missing}'


def test_chat_vocabulary_is_exactly_the_safe_one_shot_clips():
    expected = {
        name for name, info in ACTIONS.items()
        if info.get('mode') == 'once' and name not in EXCLUDED
    }
    assert CHAT_ACTIONS == expected, (
        f'missing: {sorted(expected - CHAT_ACTIONS)}; '
        f'unexpected: {sorted(CHAT_ACTIONS - expected)}')


def test_no_loop_or_hold_clip_is_chat_playable():
    for name, info in ACTIONS.items():
        if info.get('mode') in ('loop', 'hold'):
            assert name not in CHAT_ACTIONS, (
                f'{name} is {info["mode"]}-mode; a reply would never release it')


def test_special_actions_are_loop_clips_handled_by_the_app():
    assert SPECIAL_ACTIONS == {'idle', 'walk'}
    for name in SPECIAL_ACTIONS:
        assert ACTIONS[name]['mode'] == 'loop', f'{name} is not a loop clip'
        assert name not in CHAT_ACTIONS, f'{name} is driven by the app, not as a clip'


def test_allowed_covers_no_action_and_specials():
    assert allowed() == frozenset(NO_ACTION | SPECIAL_ACTIONS | CHAT_ACTIONS)
    assert '' in allowed() and 'none' in allowed()


def test_prompt_list_names_every_playable_action_and_nothing_else():
    listed = set(prompt_list().split('/'))
    assert listed == SPECIAL_ACTIONS | CHAT_ACTIONS
    assert 'none' not in listed and '' not in listed
    # Stable between runs: the prompt text must not reshuffle on every import.
    assert prompt_list() == '/'.join(sorted(listed))
