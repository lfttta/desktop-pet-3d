import json
from pathlib import Path
import struct


ROOT = Path(__file__).resolve().parents[2] / "experiments/stage2-recovery"
ACTIONS = {"idle", "walk", "turn", "wave", "pickup", "fall", "recover"}
CHARACTERS = {"godette", "alicia", "soldier"}


def read_glb_json(path: Path) -> dict:
    data = path.read_bytes()
    json_size = struct.unpack_from("<I", data, 12)[0]
    return json.loads(data[20:20 + json_size])


def test_stage2_manifest_defines_exact_public_contract():
    manifest = json.loads((ROOT / "animations/humanoid_core/manifest.json").read_text("utf-8"))
    assert manifest["id"] == "humanoid_core"
    assert manifest["version"] == 1
    assert set(manifest["actions"]) == ACTIONS
    assert all(manifest["actions"][name]["mode"] in {"loop", "once", "hold"} for name in ACTIONS)


def test_stage2_runtime_catalog_excludes_swordsman():
    catalog = json.loads((ROOT / "app/assets_data/characters.json").read_text("utf-8"))
    assert {entry["id"] for entry in catalog} == CHARACTERS
    assert "swordsman" not in {entry["id"] for entry in catalog}


def test_stage2_character_contracts_are_complete():
    required_bones = {"Hips", "Spine", "Head", "LeftUpperArm", "RightUpperArm", "LeftUpperLeg", "RightUpperLeg", "LeftFoot", "RightFoot"}
    for character_id in CHARACTERS:
        config = json.loads((ROOT / f"characters/{character_id}.json").read_text("utf-8"))
        assert config["schema_version"] == 2
        assert config["id"] == character_id
        assert config["motion_library"] == "humanoid_core@1"
        assert set(config["action_map"]) == ACTIONS
        assert required_bones <= set(config["bone_map"])
        assert set(config["capabilities"]) >= {"expressions", "blink", "mouth", "look", "secondary_motion"}
        assert len(config["foot_anchor"]) == 3
        assert config["default_height"] > 0
        assert config["locomotion"]["meters_per_cycle"] > 0
        assert set(config["action_bounds"]) == ACTIONS


def test_local_replacement_models_are_marked_non_redistributable():
    for character_id in ("alicia", "soldier"):
        config = json.loads((ROOT / f"characters/{character_id}.json").read_text("utf-8"))
        assert config["source"]["sha256"]
        assert config["source"]["redistributable"] is False
        assert config["publishable"] is False


def test_runtime_models_are_the_reviewed_replacements():
    assert (ROOT / "app/models/alicia.vrm").is_file()
    soldier = read_glb_json(ROOT / "app/models/soldier.glb")
    soldier_nodes = {node.get("name"): node for node in soldier["nodes"]}
    assert soldier_nodes["Human.008"].get("scale", [1, 1, 1]) == [1, 1, 1]
    for rejected in ("cartoon_skeleton.glb", "man.glb"):
        assert not (ROOT / "app/models" / rejected).exists()
