"""Action menu contract: the right-click categories must cover the runtime library.

The native tray menu echoes COMMAND_BASE + flat index, so this order is the
contract between app/assets_data/action_menu.json and app/scripts/action_menu.gd.
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MENU_PATH = ROOT / 'app/assets_data/action_menu.json'
LIBRARY_PATH = ROOT / 'app/assets_data/humanoid_core.json'
EXPECTED_ACTIONS = 65


def load(path: Path) -> dict:
    return json.loads(path.read_text('utf-8'))


def menu_actions(menu: dict) -> list:
    """Flatten the same way ActionMenu.build does, so order matches the runtime."""
    library = load(LIBRARY_PATH)['actions']
    flat = []
    for category in menu['categories']:
        assert category['label'], 'category label is required'
        listed = category['actions']
        assert listed, f"{category.get('id')}: category must list actions"
        kept = [name for name in listed if name in library and name not in flat]
        assert kept, f"{category['label']}: no action of this category exists in the library"
        flat.extend(kept)
    return flat


def test_menu_parses_and_declares_library():
    menu = load(MENU_PATH)
    assert menu['schema_version'] == 1
    assert menu['library'] == 'humanoid_core'
    assert menu['categories'], 'menu must declare categories'


def test_categories_are_unique_and_labelled():
    menu = load(MENU_PATH)
    ids = [category['id'] for category in menu['categories']]
    labels = [category['label'] for category in menu['categories']]
    assert len(ids) == len(set(ids)), f'duplicate category id: {ids}'
    assert len(labels) == len(set(labels)), f'duplicate category label: {labels}'


def test_menu_covers_every_library_action_exactly_once():
    menu = load(MENU_PATH)
    library = set(load(LIBRARY_PATH)['actions'])
    flat = menu_actions(menu)
    assert len(library) == EXPECTED_ACTIONS, f'library changed: {len(library)} actions'
    assert len(flat) == len(set(flat)), 'an action is listed in two categories'
    assert set(flat) == library, (
        f'menu is missing {sorted(library - set(flat))}; '
        f'menu has extra {sorted(set(flat) - library)}'
    )


def test_every_action_is_selectable_and_reachable():
    """Every library action maps to a unique command inside the native range."""
    menu = load(MENU_PATH)
    flat = menu_actions(menu)
    commands = {1000 + index: name for index, name in enumerate(flat)}
    assert len(commands) == EXPECTED_ACTIONS
    assert min(commands) == 1000, 'first action command must be the documented base'
    assert set(commands.values()) == set(load(LIBRARY_PATH)['actions'])
    # Command 1000 + 65 must stay outside the action range.
    assert 1000 + len(flat) not in commands


def test_core_category_keeps_the_state_machine_actions():
    """The seven actions the behaviour state machine drives stay easy to reach."""
    menu = load(MENU_PATH)
    core = next(category for category in menu['categories'] if category['id'] == 'core')
    assert core['actions'] == ['idle', 'walk', 'turn', 'wave', 'pickup', 'fall', 'recover']
