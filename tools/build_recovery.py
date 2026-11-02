"""Build the recovery candidate in isolation and gate it against the stable app."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

from prepare_recovery_workspace import BASELINE, ROOT, WORKSPACE, hashes, prepare
from verify_stage2_package import verify_package
from verify_recovery_visuals import verify as verify_visuals

ENGINE = Path(r"D:\danjigame\GODDOT  WORD\Godot_v4.7.2-stable_win64_console.exe")
BLENDER = Path(r"D:\steam\steamapps\common\Blender\blender.exe")
REPORT = WORKSPACE / "reports"
PACKAGE = WORKSPACE / "package"


def run(name: str, arguments: list[str], marker: str = "", timeout: int = 300) -> str:
    REPORT.mkdir(parents=True, exist_ok=True)
    path = REPORT / f"{name}.log"
    with path.open("w", encoding="utf-8") as output:
        completed = subprocess.run(arguments, cwd=ROOT, stdout=output, stderr=subprocess.STDOUT,
                                   timeout=timeout, creationflags=subprocess.CREATE_NO_WINDOW)
    result = path.read_text("utf-8", errors="replace")
    if completed.returncode or (marker and marker not in result) or "SCRIPT ERROR:" in result or "\nERROR:" in result:
        raise RuntimeError(f"{name} failed ({completed.returncode}):\n{result[-6000:]}")
    print(f"PASS {name}", flush=True)
    return result


def godot(name: str, script: str, marker: str, render: bool = False) -> None:
    command = [str(ENGINE), "--path", str(WORKSPACE / "app")]
    if not render:
        command.append("--headless")
    run(name, command + ["--script", str(ROOT / script)], marker)


def stable_unchanged() -> None:
    expected = hashes(BASELINE / "app")
    current = hashes(ROOT / "app")
    differences = [name for name, digest in expected.items() if name != "export_presets.cfg" and current.get(name) != digest]
    if differences:
        raise RuntimeError("Stable app changed: " + ", ".join(differences))
    (REPORT / "stable-integrity.json").write_text(json.dumps({"checked_files": len(expected) - 1,
        "changed_files": differences, "expected_config_change": "export_presets.cfg allows the original Godette cache and targets stable-godette"}, indent=2), "utf-8")


def main() -> None:
    prepare()
    REPORT.mkdir(parents=True, exist_ok=True)
    stable_unchanged()
    run("prepare", [sys.executable, "-B", str(ROOT / "tools/assets/prepare_stage2.py")])
    run("convert", [str(BLENDER), "--background", "--factory-startup", "--python-exit-code", "1",
                    "--python", str(ROOT / "tools/assets/convert_stage2.py")])
    run("asset-tests", [sys.executable, "-B", "-m", "pytest", "tests/assets/test_stage2_contract.py",
                        "tests/assets/test_stage2_sources.py", "-q", "-p", "no:cacheprovider"])
    run("import", [str(ENGINE), "--headless", "--path", str(WORKSPACE / "app"), "--editor", "--import", "--quit"])
    godot("bake", "tools/assets/bake_stage2.gd", "STAGE2_BAKED")
    godot("baseline", "tools/assets/verify_recovery.gd", "GODETTE_REGRESSION failures=0")
    godot("runtime", "tools/assets/verify_recovery_runtime.gd", "failures=0")
    godot("render", "tools/assets/render_recovery.gd", "RECOVERY_RENDER frames=210 framing_failures=0", render=True)
    godot("preview-ui", "tools/assets/verify_recovery_ui.gd", "RECOVERY_UI failures=0", render=True)
    run("service-tests", [sys.executable, "-B", "-m", "pytest", "tests/service", "-q", "-p", "no:cacheprovider",
                          "--basetemp", str(WORKSPACE / "build/test-service")])
    PACKAGE.mkdir(parents=True, exist_ok=True)
    run("service-build", [sys.executable, "-B", str(ROOT / "tools/build_service.py"), "--source-root", str(WORKSPACE), "--output", str(PACKAGE / "service")])
    run("frozen-service-tests", [sys.executable, "-B", "-m", "pytest", "tests/service/test_process.py", "-q", "-p", "no:cacheprovider",
                                 "--service-exe", str(PACKAGE / "service/pet-service.exe"), "--basetemp", str(WORKSPACE / "build/test-frozen")])
    preset = (ROOT / "tools/runtime/export_presets.cfg").read_text("utf-8")
    (WORKSPACE / "app/export_presets.cfg").write_text(preset, "utf-8")
    run("export", [str(ENGINE), "--headless", "--path", str(WORKSPACE / "app"), "--export-release", "Windows", str(PACKAGE / "Pet3D.exe")])
    shutil.copy2(ROOT / "tools/runtime/README-recovery.md", PACKAGE / "README-stage2.md")
    shutil.copy2(ROOT / "THIRD_PARTY_NOTICES.md", PACKAGE / "THIRD_PARTY_NOTICES.md")
    shutil.copy2(ROOT / "tools/runtime/Preview.cmd", PACKAGE / "Preview.cmd")
    files = [{"path": path.relative_to(PACKAGE).as_posix(), "size": path.stat().st_size,
              "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
             for path in sorted(PACKAGE.rglob("*")) if path.is_file() and path.name != "manifest.json"]
    (PACKAGE / "manifest.json").write_text(json.dumps(files, indent=2), "utf-8")
    verify_package(PACKAGE, REPORT)
    verify_visuals(PACKAGE, REPORT / "packaged-visuals")
    stable_unchanged()
    stale = (ROOT / "dist/stage-2").resolve()
    quarantine = (ROOT / "build/rejected/stage-2-before-recovery").resolve()
    if stale.is_dir() and not quarantine.exists():
        if stale.parent != (ROOT / "dist").resolve() or not quarantine.is_relative_to(ROOT.resolve()):
            raise RuntimeError("Archive path outside workspace")
        quarantine.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(stale), str(quarantine))
    print("RECOVERY_BUILD_OK " + str(PACKAGE), flush=True)


if __name__ == "__main__":
    main()
