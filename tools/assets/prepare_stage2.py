"""Generate runtime character contracts from repository-owned contracts and manifests."""
from __future__ import annotations

import json
from pathlib import Path
import struct

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "animations/humanoid_core/manifest.json"
RUNTIME_DATA = ROOT / "app/assets_data"
DEV_FIELDS = {"rest", "inspected_meshes", "source", "import_options"}
MOTION_LIBRARY_PREFIX = "humanoid_core"
# Single explicit decision point: the delivered package ships exactly these characters.
PACKAGE_CHARACTER_IDS = ["godette"]
EXPRESSION_PRESETS = {"neutral": "neutral", "happy": "joy", "sad": "sorrow", "blink": "blink", "mouth_a": "a"}


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Explicit LF: a Windows checkout must not turn every generated file into CRLF.
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", "utf-8", newline="\n")


def load_manifest() -> dict:
    return json.loads(MANIFEST.read_text("utf-8"))


def is_runtime_contract(config) -> bool:
    return (isinstance(config, dict) and config.get("schema_version", 0) >= 2
            and str(config.get("motion_library", "")).startswith(MOTION_LIBRARY_PREFIX))


def sync_actions(config: dict, manifest: dict) -> None:
    """The manifest owns the action set: every contract declares exactly those
    actions, in manifest order. Bounds a contract already tuned are kept; new
    actions inherit its first bound as the conservative box."""
    actions = list(manifest["actions"])
    tuned = config.get("action_bounds", {})
    template = next(iter(tuned.values()), {"min": [-1.4, -0.3, -1.4], "max": [1.4, 2.1, 1.4]})
    config["animations"] = actions
    config["action_map"] = {name: name for name in actions}
    config["action_bounds"] = {name: tuned.get(name, template) for name in actions}


def discover_contracts(manifest: dict) -> dict[str, tuple[Path, dict]]:
    """Read every qualifying contract under characters/; the manifest stays the
    single source of truth for the action set and its key order."""
    contracts: dict[str, tuple[Path, dict]] = {}
    for path in sorted(ROOT.glob("characters/*.json")):
        # Local-only samples are development input, never runtime contracts.
        if path.name.endswith(".local.json"):
            continue
        config = json.loads(path.read_text("utf-8"))
        if not is_runtime_contract(config):
            continue
        for name in config.get("animations", []):
            if name not in manifest["actions"]:
                raise ValueError(f"{path.name}: animation '{name}' is outside the manifest")
        for source in config.get("action_map", {}).values():
            if source not in manifest["actions"]:
                raise ValueError(f"{path.name}: action_map target '{source}' is outside the manifest")
        for name in config.get("action_bounds", {}):
            if name not in manifest["actions"]:
                raise ValueError(f"{path.name}: action_bounds key '{name}' is outside the manifest")
        character_id = str(config.get("id", ""))
        if not character_id:
            raise ValueError(f"{path.name}: qualifying contract needs an id")
        contracts[character_id] = (path, config)
    return contracts


def extract_expressions(config: dict) -> dict:
    """Parse the VRM0 blendShapeMaster only for contracts that declare the
    capability; every other model passes its own expressions through untouched."""
    # Contracts historically record the format in `source_format`; `format` is a
    # legacy alias. Either spelling must gate the VRM0 parser.
    declared_format = config.get("source_format", config.get("format"))
    if not config.get("capabilities", {}).get("expressions") or declared_format != "vrm0":
        return config.get("expressions", {})
    blob = (ROOT / "app" / str(config["model_path"]).removeprefix("res://")).read_bytes()
    doc = json.loads(blob[20:20 + struct.unpack_from("<I", blob, 12)[0]])
    groups = {group["presetName"]: group for group in doc["extensions"]["VRM"]["blendShapeMaster"]["blendShapeGroups"]}
    expressions = {}
    for name, preset in EXPRESSION_PRESETS.items():
        bindings = []
        for bind in groups[preset]["binds"]:
            if doc["meshes"][bind["mesh"]]["name"] != "FaceMesh":
                raise ValueError("Unexpected expression mesh; inspect the imported model")
            bindings.append({"mesh_path": "Skeleton3D/Face", "index": bind["index"],
                             "name": f"morph_{bind['index']}", "weight": bind["weight"] / 100})
        expressions[name] = bindings
    return expressions


def main() -> None:
    manifest = load_manifest()
    contracts = discover_contracts(manifest)
    for character_id in PACKAGE_CHARACTER_IDS:
        if character_id not in contracts:
            raise ValueError(f"Package character {character_id} has no qualifying contract")
    for character_id, (path, config) in contracts.items():
        # The contract keeps the same action set the manifest enumerates, and the
        # synced form is written back so bake, review and runtime agree.
        sync_actions(config, manifest)
        write_json(path, config)
        # Runtime carries only the contract, not development snapshots or source paths.
        runtime = {key: value for key, value in config.items() if key not in DEV_FIELDS}
        # Assigning an existing key keeps the contract's key order stable.
        runtime["expressions"] = extract_expressions(config)
        write_json(RUNTIME_DATA / f"{character_id}.json", runtime)
    write_json(RUNTIME_DATA / "characters.json", [
        {"id": character_id, "name": character_id.capitalize(),
         "config": f"res://assets_data/{character_id}.json"}
        for character_id in PACKAGE_CHARACTER_IDS
    ])
    write_json(RUNTIME_DATA / "humanoid_core.json", manifest)


if __name__ == "__main__":
    main()
