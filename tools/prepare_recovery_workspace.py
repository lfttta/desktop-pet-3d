"""Snapshot the stable app and create an isolated animation recovery project."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT / "experiments/stage2-recovery"
BASELINE = ROOT / "backups/godette-before-recovery"
MARKER = ".recovery-workspace.json"


def hashes(directory: Path) -> dict[str, str]:
    return {
        path.relative_to(directory).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.rglob("*"))
        if path.is_file() and ".godot" not in path.parts and "__pycache__" not in path.parts
    }


def prepare() -> Path:
    if not BASELINE.exists():
        BASELINE.mkdir(parents=True)
        for name in ("app", "service", "characters", "animations", "tools"):
            shutil.copytree(ROOT / name, BASELINE / name,
                            ignore=shutil.ignore_patterns(".godot", "__pycache__", ".tmp*"))
        (BASELINE / "sha256.json").write_text(json.dumps(hashes(BASELINE), indent=2), "utf-8")
    if not WORKSPACE.exists():
        WORKSPACE.mkdir(parents=True)
        for name in ("app", "service", "characters", "animations"):
            shutil.copytree(BASELINE / name, WORKSPACE / name)
        (WORKSPACE / MARKER).write_text(json.dumps({"baseline": str(BASELINE), "purpose": "isolated-stage2-recovery"}), "utf-8")
    if not (WORKSPACE / MARKER).is_file():
        raise RuntimeError("Recovery workspace marker missing; refusing to overwrite an unrelated directory")
    shutil.copy2(BASELINE / "app/scripts/character_view.gd", WORKSPACE / "app/scripts/stable_character_view.gd")
    runtime = ROOT / "tools/runtime/character_view.gd"
    if runtime.is_file():
        shutil.copy2(runtime, WORKSPACE / "app/scripts/character_view.gd")
    shutil.copy2(ROOT / "tools/runtime/review.gd", WORKSPACE / "app/scripts/review.gd")
    main_path = WORKSPACE / "app/scripts/main.gd"
    main_text = (BASELINE / "app/scripts/main.gd").read_text("utf-8")
    main_text = main_text.replace(
        '\t_parse_options()\n',
        '\t_parse_options()\n\tif options.has("review"):\n\t\tset_process(false)\n\t\tadd_child(load("res://scripts/review.gd").new())\n\t\treturn\n', 1)
    main_text = main_text.replace(
        '\tsuper._change_character(index)\n',
        '\tsuper._change_character(index)\n\tif is_instance_valid(actor) and actor.has_signal("visible_bounds_changed") and not actor.is_connected("visible_bounds_changed", _constrain_window):\n\t\tactor.connect("visible_bounds_changed", _constrain_window)\n', 1)
    main_path.write_text(main_text, "utf-8")
    return WORKSPACE


if __name__ == "__main__":
    print(prepare())
