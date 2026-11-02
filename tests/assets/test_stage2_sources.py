import json
import struct
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2] / "experiments/stage2-recovery"
ACTIONS = {"idle", "walk", "turn", "wave", "pickup", "fall", "recover"}


def read_glb_json(path: Path):
    data = path.read_bytes()
    assert data[:4] == b"glTF"
    json_size = struct.unpack_from("<I", data, 12)[0]
    return json.loads(data[20:20 + json_size])


def test_stage2_source_manifest_has_seven_real_inputs():
    manifest = json.loads((ROOT / "animations/humanoid_core/source_manifest.json").read_text("utf-8"))
    assert set(manifest["clips"]) == ACTIONS
    for name, clip in manifest["clips"].items():
        assert clip["source_sha256"]
        assert clip["source_file"].endswith(".fbx")
        assert clip["semantic_status"] in {"verified", "authored"}


def test_normalized_stage2_clips_are_meter_y_up_and_named():
    for name in ACTIONS:
        doc = read_glb_json(ROOT / f"build/stage2/motions/{name}.glb")
        rig = next(node for node in doc["nodes"] if node.get("name") == "SourceRig")
        assert rig.get("scale", [1, 1, 1]) == [1, 1, 1]
        assert rig.get("rotation", [0, 0, 0, 1]) == [0, 0, 0, 1]
        assert [animation["name"] for animation in doc["animations"]] == [name]
        assert doc["asset"]["extras"]["units"] == "meters"
        assert doc["asset"]["extras"]["up"] == "+Y"
