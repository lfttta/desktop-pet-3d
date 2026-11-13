"""动作元数据一致性：manifest ↔ action_map ↔ 动画库 ↔ 托盘菜单。

58 个新动作的 mode/root_motion 当初按文件名推断，这里把结构性不变量钉死，
让回归在桌面人工复核之前就能暴露（滑步风险主要来自行走族 root_motion 口径）。
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / 'animations/humanoid_core/manifest.json'
GODETTE = ROOT / 'app/assets_data/godette.json'
MENU = ROOT / 'app/assets_data/action_menu.json'

LOCOMOTION_ACTIONS = {'walk', 'running', 'old_man_walk', 'holding_walk', 'box_walk_arc'}
PHYSICS_ACTIONS = {'pickup', 'fall', 'recover'}
VALID_MODES = {'once', 'loop', 'hold'}
VALID_ROOT_MOTION = {'none', 'vertical_only', 'preserve'}


def load_actions() -> dict:
    return json.loads(MANIFEST.read_text('utf-8'))['actions']


def load_godette() -> dict:
    return json.loads(GODETTE.read_text('utf-8'))


def test_action_map_matches_manifest():
    actions = load_actions()
    action_map = load_godette()['action_map']
    assert len(actions) == 65
    assert set(action_map) == set(actions)


def test_action_map_targets_exist_in_library():
    data = load_godette()
    missing = [a for a, anim in data['action_map'].items() if anim not in data['animations']]
    assert not missing, f'action_map targets missing from animations: {missing}'


def test_mode_values_valid():
    bad = {k: v.get('mode') for k, v in load_actions().items() if v.get('mode') not in VALID_MODES}
    assert not bad, f'invalid mode: {bad}'


def test_root_motion_values_valid():
    bad = {k: v.get('root_motion') for k, v in load_actions().items()
           if v.get('root_motion') not in VALID_ROOT_MOTION}
    assert not bad, f'invalid root_motion: {bad}'


def test_locomotion_is_loop_in_place():
    # 阶段三口径：行走族必须是 loop + vertical_only 的原地动画，
    # 水平位移由窗口平移驱动；改成 preserve 会脚底打滑。
    actions = load_actions()
    for name in sorted(LOCOMOTION_ACTIONS):
        assert name in actions, f'locomotion action missing: {name}'
        assert actions[name]['mode'] == 'loop', name
        assert actions[name]['root_motion'] == 'vertical_only', name


def test_idle_is_loop():
    assert load_actions()['idle']['mode'] == 'loop'


def test_physics_actions_present():
    action_map = load_godette()['action_map']
    assert PHYSICS_ACTIONS <= set(action_map)


def test_labels_present():
    missing = [k for k, v in load_actions().items() if not str(v.get('label', '')).strip()]
    assert not missing, f'actions without label: {missing}'


def test_blend_seconds_nonnegative():
    bad = {k: v.get('blend_seconds') for k, v in load_actions().items()
           if not isinstance(v.get('blend_seconds', 0), (int, float)) or v.get('blend_seconds', 0) < 0}
    assert not bad, f'invalid blend_seconds: {bad}'


def test_menu_references_valid_and_unique():
    action_map = load_godette()['action_map']
    menu = json.loads(MENU.read_text('utf-8'))
    seen: set = set()
    for category in menu['categories']:
        for action in category['actions']:
            assert action in action_map, f'menu references unknown action: {action}'
            assert action not in seen, f'duplicate menu entry: {action}'
            seen.add(action)