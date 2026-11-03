"""Prepare the single Godette contract from repository-owned resources."""
from __future__ import annotations

import json
from pathlib import Path
import struct

ROOT = Path(__file__).resolve().parents[2]
ACTIONS = {
    "idle": {"mode": "loop", "source": "Idle.fbx", "blend_seconds": 0.15, "root_motion": "preserve"},
    "walk": {"mode": "loop", "source": "Walking.fbx", "blend_seconds": 0.15, "root_motion": "vertical_only"},
    "turn": {"mode": "once", "source": "Idle.fbx", "blend_seconds": 0.12, "root_motion": "none", "derived": "turn_step", "heading_degrees": 180},
    "wave": {"mode": "once", "source": "Waving.fbx", "blend_seconds": 0.15, "root_motion": "preserve"},
    "pickup": {"mode": "hold", "source": "Idle.fbx", "blend_seconds": 0.12, "root_motion": "none", "derived": "suspended"},
    "fall": {"mode": "hold", "source": "Falling.fbx", "blend_seconds": 0.08, "root_motion": "none"},
    "recover": {"mode": "once", "source": "Idle.fbx", "blend_seconds": 0.12, "root_motion": "vertical_only", "derived": "landing_cushion"},
}


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", "utf-8")


def main() -> None:
    config = json.loads((ROOT / "characters/godette.json").read_text("utf-8"))
    blob = (ROOT / "app/models/godette.vrm").read_bytes()
    doc = json.loads(blob[20:20 + struct.unpack_from("<I", blob, 12)[0]])
    groups = {group["presetName"]: group for group in doc["extensions"]["VRM"]["blendShapeMaster"]["blendShapeGroups"]}
    expressions = {}
    for name, preset in {"neutral": "neutral", "happy": "joy", "sad": "sorrow", "blink": "blink", "mouth_a": "a"}.items():
        bindings = []
        for bind in groups[preset]["binds"]:
            if doc["meshes"][bind["mesh"]]["name"] != "FaceMesh":
                raise ValueError("Unexpected expression mesh; inspect the imported model")
            bindings.append({"mesh_path": "Skeleton3D/Face", "index": bind["index"],
                             "name": f"morph_{bind['index']}", "weight": bind["weight"] / 100})
        expressions[name] = bindings
    config.update({
        "schema_version": 2, "source_format": "vrm0", "skeleton_profile": "humanoid",
        "motion_library": "humanoid_core@1", "action_map": {name: name for name in ACTIONS},
        "animation_library": "res://resources/animations/godette.tres", "animations": list(ACTIONS),
        "foot_anchor": [0, 0, 0], "default_height": 1.6,
        "locomotion": {"meters_per_cycle": 1.15, "nominal_speed": 0.9},
        "action_bounds": {name: {"min": [-1.4, -0.3, -1.4], "max": [1.4, 2.1, 1.4]} for name in ACTIONS},
        "capabilities": {"expressions": True, "blink": True, "mouth": True, "look": False, "secondary_motion": True},
        "expressions": expressions,
    })
    write_json(ROOT / "characters/godette.json", config)
    # Runtime carries only the contract, not development snapshots or source paths.
    runtime = {key: value for key, value in config.items() if key not in {"rest", "inspected_meshes", "source", "import_options"}}
    write_json(ROOT / "app/assets_data/godette.json", runtime)
    write_json(ROOT / "app/assets_data/characters.json", [{"id": "godette", "name": "Godette", "config": "res://assets_data/godette.json"}])
    manifest = {"schema_version": 1, "id": "humanoid_core", "version": 1, "actions": ACTIONS}
    write_json(ROOT / "animations/humanoid_core/manifest.json", manifest)
    write_json(ROOT / "app/assets_data/humanoid_core.json", manifest)


if __name__ == "__main__":
    main()
