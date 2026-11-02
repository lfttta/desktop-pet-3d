"""Generate Stage 2 manifests and runtime character contracts."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil


ROOT = Path(__file__).resolve().parents[2] / "experiments/stage2-recovery"
AZPEPOZE_MODELS = Path(r"D:\danjigame\3D-pet\work-pet\prototypes\azpepoze\models")
ALICIA_SOURCE = AZPEPOZE_MODELS / "AliciaSolid_vrm-0.51.vrm"
SOLDIER_SOURCE = AZPEPOZE_MODELS / "soldier.glb"
ACTIONS = {
    "idle": {"mode": "loop", "source": "Idle.fbx", "blend_seconds": 0.15, "root_motion": "preserve"},
    "walk": {"mode": "loop", "source": "Walking.fbx", "blend_seconds": 0.15, "root_motion": "vertical_only"},
    "turn": {"mode": "once", "source": "Idle.fbx", "blend_seconds": 0.12, "root_motion": "none", "derived": "turn_step", "heading_degrees": 180},
    "wave": {"mode": "once", "source": "Waving.fbx", "blend_seconds": 0.15, "root_motion": "preserve"},
    "pickup": {"mode": "hold", "source": "Idle.fbx", "blend_seconds": 0.12, "root_motion": "none", "derived": "suspended"},
    "fall": {"mode": "hold", "source": "Falling.fbx", "blend_seconds": 0.08, "root_motion": "none"},
    "recover": {"mode": "once", "source": "Idle.fbx", "blend_seconds": 0.12, "root_motion": "vertical_only", "derived": "landing_cushion"},
}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def action_bounds() -> dict:
    return {name: {"min": [-0.8, -0.05, -0.45], "max": [0.8, 2.05, 0.45]} for name in ACTIONS}


def upgrade_godette() -> dict:
    config = json.loads((ROOT / "characters/godette.json").read_text("utf-8"))
    config.update({
        "schema_version": 2, "source_format": "vrm0", "skeleton_profile": "humanoid",
        "motion_library": "humanoid_core@1", "action_map": {name: name for name in ACTIONS},
        "foot_anchor": [0.0, 0.0, 0.0], "default_height": 1.6,
        "locomotion": {"meters_per_cycle": 1.15, "nominal_speed": 0.9},
        "action_bounds": action_bounds(),
        "capabilities": {"expressions": True, "blink": True, "mouth": True, "look": True, "secondary_motion": True},
        "animation_library": "res://resources/animations/godette.tres", "animations": list(ACTIONS),
    })
    return config


def character_contract(
    character_id: str,
    model_path: str,
    source: Path,
    source_format: str,
    skeleton_path: str,
    bone_map: dict,
    source_map: dict,
    yaw: float,
    bounds_min: list[float],
    bounds_max: list[float],
    provenance_status: str,
) -> dict:
    height = bounds_max[1] - bounds_min[1]
    return {
        "schema_version": 2, "id": character_id, "model_path": model_path,
        "source_format": source_format, "skeleton_profile": "humanoid", "skeleton_path": skeleton_path,
        "bone_map": bone_map, "source_bone_map": {key: source_map[key] for key in bone_map if key in source_map},
        "source_to_target_yaw_degrees": yaw,
        "motion_library": "humanoid_core@1", "animation_library": f"res://resources/animations/{character_id}.tres",
        "animation_library_name": "shared", "action_map": {name: name for name in ACTIONS}, "animations": list(ACTIONS),
        "foot_anchor": [0.0, 0.0, 0.0], "default_height": height,
        "locomotion": {"meters_per_cycle": 1.1, "nominal_speed": 0.85}, "action_bounds": action_bounds(),
        "hit_shapes": {"mode": "humanoid_bones"},
        "capabilities": {"expressions": False, "blink": False, "mouth": False, "look": False, "secondary_motion": False},
        "expressions": {},
        "display": {
            "units": "meters", "up": "+Y", "model_forward": "-Z",
            "rotation_degrees": [0, yaw, 0], "scale": 1.0,
            "ground_offset_y": -bounds_min[1], "rest_bounds_min": bounds_min, "rest_bounds_max": bounds_max,
        },
        "source": {
            "path": str(source), "sha256": sha256(source),
            "provenance_status": provenance_status, "redistributable": False,
        },
        "publishable": False, "enabled_by_default": True,
        "notes": ["Local technical adaptation only until redistribution rights are confirmed."],
    }


def remove_stale_stage2_assets() -> None:
    stale = [
        "cartoon_skeleton.glb", "cartoon_skeleton_0.jpg", "man.glb",
        "candidate_spider_gwen.glb", "candidate_soldier.glb", "candidate_soldier_reexport.glb",
        "candidate_soldier_applied.glb", "candidate_avatar_sample_m.vrm", "candidate_alicia.vrm",
    ]
    for name in stale:
        path = ROOT / "app/models" / name
        if path.exists():
            path.unlink()
    for character_id in ("cartoon_skeleton", "man"):
        for path in (ROOT / f"characters/{character_id}.json", ROOT / f"app/assets_data/{character_id}.json"):
            if path.exists():
                path.unlink()


def main() -> None:
    if not (ROOT / ".recovery-workspace.json").is_file():
        raise RuntimeError("Run tools/prepare_recovery_workspace.py first")
    for source in (ALICIA_SOURCE, SOLDIER_SOURCE):
        if not source.is_file():
            raise FileNotFoundError(source)
    runtime_manifest = {"schema_version": 1, "id": "humanoid_core", "version": 1, "actions": ACTIONS}
    write_json(ROOT / "animations/humanoid_core/manifest.json", runtime_manifest)
    write_json(ROOT / "app/assets_data/humanoid_core.json", runtime_manifest)
    godette = upgrade_godette()
    source_map = godette["source_bone_map"]
    alicia_map = {
        "Hips": "Hips", "Spine": "Spine", "Chest": "Chest", "Neck": "Neck", "Head": "Head",
        "LeftShoulder": "LeftShoulder", "LeftUpperArm": "LeftUpperArm", "LeftLowerArm": "LeftLowerArm", "LeftHand": "LeftHand",
        "RightShoulder": "RightShoulder", "RightUpperArm": "RightUpperArm", "RightLowerArm": "RightLowerArm", "RightHand": "RightHand",
        "LeftUpperLeg": "LeftUpperLeg", "LeftLowerLeg": "LeftLowerLeg", "LeftFoot": "LeftFoot", "LeftToes": "LeftToes",
        "RightUpperLeg": "RightUpperLeg", "RightLowerLeg": "RightLowerLeg", "RightFoot": "RightFoot", "RightToes": "RightToes",
    }
    for name in source_map:
        if any(finger in name for finger in ("Thumb", "Index", "Middle", "Little")):
            alicia_map[name] = name
    soldier_map = {
        "Hips": "ch_Hips", "Spine": "ch_Spine", "Chest": "ch_Spine1", "Neck": "ch_Neck", "Head": "ch_Head",
        "LeftShoulder": "ch_LeftShoulder", "LeftUpperArm": "ch_LeftArm", "LeftLowerArm": "ch_LeftForeArm", "LeftHand": "ch_LeftHand",
        "RightShoulder": "ch_RightShoulder", "RightUpperArm": "ch_RightArm", "RightLowerArm": "ch_RightForeArm", "RightHand": "ch_RightHand",
        "LeftUpperLeg": "ch_LeftUpLeg", "LeftLowerLeg": "ch_LeftLeg", "LeftFoot": "ch_LeftFoot", "LeftToes": "ch_LeftToeBase",
        "RightUpperLeg": "ch_RightUpLeg", "RightLowerLeg": "ch_RightLeg", "RightFoot": "ch_RightFoot", "RightToes": "ch_RightToeBase",
    }
    configs = {
        "godette": godette,
        "alicia": character_contract(
            "alicia", "res://models/alicia.vrm", ALICIA_SOURCE, "vrm0", "Root/Skeleton3D",
            alicia_map, source_map, 180.0, [-0.614749, -0.001381, -0.275261], [0.61465, 1.650247, 0.358083], "embedded-metadata",
        ),
        "soldier": character_contract(
            "soldier", "res://models/soldier.glb", SOLDIER_SOURCE, "glb", "Human_008/Skeleton3D",
            soldier_map, source_map, 0.0, [-0.861244, 0.002609, -0.37347], [0.861286, 1.989168, 0.408811], "user-provided",
        ),
    }
    app_models = ROOT / "app/models"
    app_models.mkdir(parents=True, exist_ok=True)
    remove_stale_stage2_assets()
    shutil.copy2(ALICIA_SOURCE, app_models / "alicia.vrm")
    for character_id, config in configs.items():
        write_json(ROOT / f"characters/{character_id}.json", config)
        write_json(ROOT / f"app/assets_data/{character_id}.json", config)
    write_json(ROOT / "app/assets_data/characters.json", [
        {"id": "godette", "name": "Godette · VRM", "config": "res://assets_data/godette.json"},
        {"id": "alicia", "name": "Alicia · VRM", "config": "res://assets_data/alicia.json"},
        {"id": "soldier", "name": "士兵 · GLB", "config": "res://assets_data/soldier.json"},
    ])
    (ROOT / "service/character_catalog.py").write_text(
        "# Generated by tools/assets/prepare_stage2.py.\n"
        "ALLOWED_CHARACTER_IDS = frozenset({'godette', 'alicia', 'soldier'})\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
