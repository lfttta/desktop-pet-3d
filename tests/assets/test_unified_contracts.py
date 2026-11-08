"""Unified humanoid_core contract checks: manifest, character contracts, app assets."""
import json
from pathlib import Path
import struct

ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = ROOT / 'animations/humanoid_core/manifest.json'
SOURCE_MANIFEST_PATH = ROOT / 'animations/humanoid_core/source_manifest.json'
CHARACTERS_DIR = ROOT / 'characters'
SOURCE_GLB_DIR = ROOT / 'app/resources/animations/source'


def load_manifest():
    manifest = json.loads(MANIFEST_PATH.read_text('utf-8'))
    assert manifest['schema_version'] == 1
    assert manifest['id'] == 'humanoid_core'
    return manifest


def read_glb_json(path: Path) -> dict:
    data = path.read_bytes()
    assert data[:4] == b'glTF', f'{path.name}: not a GLB'
    json_size = struct.unpack_from('<I', data, 12)[0]
    return json.loads(data[20:20 + json_size])


def imported_node_name(name: str) -> str:
    """Godot's glTF importer maps the FBX namespace separator to an underscore
    (mixamorig1:Hips -> mixamorig1_Hips); contracts record the imported form."""
    return name.replace(':', '_')


def humanoid_contracts():
    """Yield (path, data) for every characters/*.json contract bound to humanoid_core."""
    for path in sorted(CHARACTERS_DIR.glob('*.json')):
        if path.name.endswith('.local.json'):
            continue
        data = json.loads(path.read_text('utf-8'))
        if not isinstance(data, dict):
            continue
        if data.get('schema_version', 0) < 2:
            continue
        if not str(data.get('motion_library', '')).startswith('humanoid_core'):
            continue
        yield path, data


def test_manifest_actions_have_source_glbs():
    """(a) Every manifest action has <id>.glb in app/resources/animations/source/.

    The 7 pipeline clips in source_manifest.json (4 converted + 3 derived:
    turn/pickup/recover) are reconciled one by one; the actions added for the
    remaining FBX sources must all be baked as well.
    """
    manifest = load_manifest()
    actions = manifest['actions']
    assert actions, 'manifest must define actions'
    source_manifest = json.loads(SOURCE_MANIFEST_PATH.read_text('utf-8'))
    assert source_manifest['schema_version'] == 1
    for clip_id, clip in source_manifest['clips'].items():
        assert clip_id in actions, f'source_manifest clip {clip_id} missing from manifest actions'
        glb = SOURCE_GLB_DIR / Path(clip['output']).name
        assert glb.is_file(), f'pipeline clip {clip_id}: missing {glb.name}'
    for action_id in actions:
        glb = SOURCE_GLB_DIR / f'{action_id}.glb'
        assert glb.is_file(), f'manifest action {action_id}: missing {glb.name}'


def test_source_bone_maps_resolve_in_source_glbs():
    """(e) Every source_bone_map value must name a bone of every converted source rig.

    bake_stage2.gd resolves each profile with Skeleton3D.find_bone(); an unknown
    name is skipped silently, so a broken mapping would not fail the bake but
    would silently drop that bone from every retargeted track.
    """
    nodes_by_clip = {
        action_id: {imported_node_name(str(node.get('name', ''))) for node in read_glb_json(SOURCE_GLB_DIR / f'{action_id}.glb')['nodes']}
        for action_id in load_manifest()['actions']
    }
    contracts = list(humanoid_contracts())
    assert contracts, 'expected at least one humanoid_core contract in characters/'
    for path, data in contracts:
        values = {imported_node_name(str(value)) for value in data['source_bone_map'].values()}
        assert values, f'{path.name}: source_bone_map must not be empty'
        for action_id, nodes in nodes_by_clip.items():
            missing = sorted(values - nodes)
            assert not missing, f'{path.name}: {action_id}.glb misses source bones {missing[:5]}'


def test_humanoid_contracts_match_canonical_bones_and_manifest():
    """(b) bone_map and source_bone_map share the godette 45-key baseline;
    animations/action_map cover exactly the manifest action set; the yaw
    field exists.

    Every humanoid_core contract is synced to the manifest by
    tools/assets/prepare_stage2.py::sync_actions, so equality is the invariant:
    a contract that quietly carries a subset would ship a runtime that exposes
    fewer actions than the library it was baked from.
    """
    baseline = json.loads((CHARACTERS_DIR / 'godette.json').read_text('utf-8'))
    bone_keys = set(baseline['bone_map'])
    assert len(bone_keys) == 45, 'godette baseline must hold the canonical 45 bones'
    manifest_actions = set(load_manifest()['actions'])
    contracts = list(humanoid_contracts())
    assert contracts, 'expected at least one humanoid_core contract in characters/'
    for path, data in contracts:
        assert set(data['bone_map']) == bone_keys, f'{path.name}: bone_map differs from godette baseline'
        assert set(data['source_bone_map']) == bone_keys, f'{path.name}: source_bone_map differs from godette baseline'
        assert 'source_to_target_yaw_degrees' in data, f'{path.name}: source_to_target_yaw_degrees missing'
        animations = set(data['animations'])
        action_map = set(data['action_map'])
        assert animations, f'{path.name}: animations must not be empty'
        assert animations == action_map, f'{path.name}: animations and action_map disagree'
        assert animations == manifest_actions, f'{path.name}: actions differ from the manifest: {sorted(manifest_actions - animations)[:5]} missing, {sorted(animations - manifest_actions)[:5]} extra'


def test_runtime_catalog_lists_only_godette():
    """(c) app/assets_data/characters.json exposes exactly ["godette"]."""
    catalog = json.loads((ROOT / 'app/assets_data/characters.json').read_text('utf-8'))
    assert [entry['id'] for entry in catalog] == ['godette']


def test_export_presets_exclude_development_assets():
    """(d) export_presets.cfg keeps test fixtures and source assets out of the package.

    The repository ships one character (Godette); the other character exclusions
    were removed with their assets, so only the development paths remain.
    """
    text = (ROOT / 'app/export_presets.cfg').read_text('utf-8')
    exclude_lines = [line for line in text.splitlines() if line.strip().startswith('exclude_filter=')]
    assert exclude_lines, 'export_presets.cfg must define exclude_filter'
    excluded = exclude_lines[0].lower()
    for required in ('tests/*', 'motions/*', 'resources/animations/source/*'):
        assert required in excluded, f'exclude_filter must drop {required}'


def test_repository_ships_exactly_one_character():
    """(e) Only Godette's model and contracts remain in the tree."""
    models = sorted(path.name for path in (ROOT / 'app/models').glob('*.vrm'))
    assert models == ['godette.vrm'], f'unexpected VRM models: {models}'
    assert not list((ROOT / 'app/models').glob('*.glb')), 'no loose GLB models should remain'
    assert sorted(path.stem for path in (ROOT / 'characters').glob('*.json')) == ['godette']
    assert sorted(path.stem for path in (ROOT / 'app/assets_data').glob('*.json')) == [
        'action_menu', 'characters', 'godette', 'humanoid_core']
