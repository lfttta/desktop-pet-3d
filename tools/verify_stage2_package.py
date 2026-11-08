"""Verify Stage 2 package resources and Chinese-space-path startup."""
from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from verify_stage1_package import (
    launch_app,
    process_is_running,
    service_exchange,
    wait_app,
    wait_pid_gone,
    wait_report,
    verify_single_instance,
    verify_service_restart,
    verify_parent_death,
    verify_corrupt_recovery,
)

ROOT = Path(__file__).resolve().parents[1]
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

def assert_clean_log(path: Path) -> None:
    clean = re.sub(r"\x1b\[[0-9;]*m", "", path.read_text("utf-8", errors="replace"))
    errors = [line for line in clean.splitlines() if line.lstrip().startswith(("ERROR:", "SCRIPT ERROR:"))]
    if errors:
        raise AssertionError("Godot errors:\n" + "\n".join(errors[-12:]))

def run_export_check(package: Path, report: Path, label: str) -> None:
    log = report / f"{label}.log"
    engine_log = report / f"{label}-engine.log"
    command = [str(package / "Pet3D.exe"), "--headless", "--log-file", str(engine_log.resolve()), "--", "--verify-package=" + str((ROOT / "tests/verify_stage2_export.gd").resolve())]
    with log.open("w", encoding="utf-8") as output:
        result = subprocess.run(command, cwd=package, stdout=output, stderr=subprocess.STDOUT, timeout=120, creationflags=CREATE_NO_WINDOW)
    text = log.read_text("utf-8", errors="replace")
    if result.returncode or "STAGE2_EXPORT_TEST failures=0" not in text:
        raise AssertionError(f"{label} failed with exit {result.returncode}")
    assert_clean_log(log)

def run_runtime_smoke(package: Path, report: Path, root: Path) -> None:
    data_dir = root / "阶段二 设置"
    replies = service_exchange(package, data_dir, [
        {"version": 1, "id": "hello", "type": "hello", "payload": {"protocol_version": 1}},
        {"version": 1, "id": "save", "type": "settings.update", "payload": {
            "user_name": "阶段二 中文", "character_id": "godette", "scale": 1.1,
        }},
        {"version": 1, "id": "stop", "type": "shutdown", "payload": {}},
    ])
    if [(item.get("id"), item.get("type")) for item in replies] != [("hello", "ready"), ("save", "result"), ("stop", "result")]:
        raise AssertionError(f"Unexpected Stage 2 service replies: {replies}")
    status_path = root / "stage2-runtime.json"
    log = report / "stage2-runtime-smoke.log"
    app = launch_app(package, data_dir, log, status_path, 3.5)
    ready = wait_report(status_path, lambda value: value.get("service_state") == "ready")
    settings = ready.get("saved_settings", {})
    details = ready.get("character_details", {})
    if settings.get("character_id") != "godette" or details.get("id") != "godette" or settings.get("user_name") != "阶段二 中文":
        raise AssertionError(f"Packaged runtime did not restore Godette: settings={settings}, details={details}")
    child = int(ready.get("service_pid", 0))
    if child <= 0 or not process_is_running(child):
        raise AssertionError("Packaged runtime did not start its service process")
    wait_app(app, log)
    wait_pid_gone(child)
    shutil.copy2(status_path, report / "stage2-runtime-smoke.json")

def verify_package(package: Path, report: Path) -> None:
    for relative in ["Pet3D.exe", "Pet3D.pck", "pet_window.windows.release.x86_64.dll", "service/pet-service.exe", "README-stage2.md", "THIRD_PARTY_NOTICES.md"]:
        if not (package / relative).is_file():
            raise AssertionError("Missing " + relative)
    manifest = json.loads((package / "manifest.json").read_text("utf-8"))
    if any(name in entry["path"].lower() for entry in manifest for name in ("swordsman", "alicia", "soldier")):
        raise AssertionError("Stage 2 manifest contains a removed character asset")
    report.mkdir(parents=True, exist_ok=True)
    run_export_check(package, report, "stage2-export-package")
    with tempfile.TemporaryDirectory(prefix="Pet3D-stage2-") as temporary:
        copied = Path(temporary) / "中文 空格" / "stage-2"
        shutil.copytree(package, copied)
        run_export_check(copied, report, "stage2-export-chinese-path")
        run_runtime_smoke(copied, report, Path(temporary))
        verify_single_instance(copied, Path(temporary), report)
        verify_service_restart(copied, Path(temporary), report)
        verify_parent_death(copied, Path(temporary), report)
        verify_corrupt_recovery(copied, Path(temporary), report)
    (report / "package-verification.json").write_text(json.dumps({
        "package": str(package.resolve()),
        "resource_check": "pass",
        "character_count": 1,
        "excluded_characters": ["alicia", "soldier", "swordsman"],
        "chinese_space_path": "pass",
        "saved_character_restore": "godette",
        "single_instance": "pass",
        "service_restart": "pass",
        "parent_death_cleanup": "pass",
        "service_cleanup": "pass",
    }, ensure_ascii=False, indent=2), encoding="utf-8")

if __name__ == "__main__":
    verify_package(ROOT / "dist/stage-2-godette", ROOT / "build/stage2/reports")
