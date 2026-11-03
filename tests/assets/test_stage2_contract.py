import json
import struct
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ACTIONS = {'idle', 'walk', 'turn', 'wave', 'pickup', 'fall', 'recover'}


def test_runtime_contains_only_godette_contract():
    catalog = json.loads((ROOT / 'app/assets_data/characters.json').read_text('utf-8'))
    assert [entry['id'] for entry in catalog] == ['godette']
    config = json.loads((ROOT / 'app/assets_data/godette.json').read_text('utf-8'))
    assert config['schema_version'] == 2
    assert set(config['animations']) == ACTIONS
    assert set(config['action_map']) == ACTIONS
    assert set(config['action_bounds']) == ACTIONS
    assert config['animation_library'] == 'res://resources/animations/godette.tres'
    assert (ROOT / 'app' / config['animation_library'].removeprefix('res://')).is_file()
    assert config['bone_map']['Hips'] == 'Hips'
    assert 'source' not in config and 'rest' not in config


def test_expressions_come_from_embedded_vrm_metadata():
    config = json.loads((ROOT / 'app/assets_data/godette.json').read_text('utf-8'))
    raw = (ROOT / 'app/models/godette.vrm').read_bytes()
    doc = json.loads(raw[20:20 + struct.unpack_from('<I', raw, 12)[0]])
    groups = {group['presetName']: group for group in doc['extensions']['VRM']['blendShapeMaster']['blendShapeGroups']}
    for name, preset in {'happy': 'joy', 'sad': 'sorrow', 'blink': 'blink', 'mouth_a': 'a', 'neutral': 'neutral'}.items():
        assert [(b['index'], b['weight']) for b in config['expressions'][name]] == [(b['index'], b['weight'] / 100) for b in groups[preset]['binds']]


def test_action_semantics_and_cache_baseline():
    manifest = json.loads((ROOT / 'animations/humanoid_core/manifest.json').read_text('utf-8'))
    assert set(manifest['actions']) == ACTIONS
    assert manifest['actions']['turn']['derived'] == 'turn_step'
    assert manifest['actions']['pickup']['derived'] == 'suspended'
    assert manifest['actions']['recover']['derived'] == 'landing_cushion'
    assert (ROOT / 'app/motions/cache/godette.tres').is_file()


def test_active_asset_tools_do_not_read_legacy_project():
    for name in ['prepare_stage2.py', 'convert_stage2.py', 'bake_stage2.gd']:
        text = (ROOT / 'tools/assets' / name).read_text('utf-8')
        assert 'work-pet' not in text
        assert 'alicia' not in text.lower() and 'soldier' not in text.lower()
