"""Build, test, export, and verify the complete stage-1 Windows package."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

from verify_stage1_package import verify_package

ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT / "app"
REPORT = ROOT / "docs" / "reports" / "stage-1"
OUTPUT = ROOT / "dist" / "stage-1"
DEFAULT_ENGINE = Path(r"D:\danjigame\GODDOT  WORD\Godot_v4.7.2-stable_win64.exe")
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def safe_clean_output(path: Path) -> None:
    resolved = path.resolve()
    expected = (ROOT / "dist" / "stage-1").resolve()
    if resolved != expected or path.is_symlink():
        raise SystemExit(f"Refusing to clean unexpected output directory: {path}")
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)


def run_command(name: str, command: list[str], timeout: int = 180, cwd: Path = ROOT) -> str:
    REPORT.mkdir(parents=True, exist_ok=True)
    log = REPORT / f"build-{name}.log"
    try:
        with log.open("w", encoding="utf-8") as output:
            result = subprocess.run(command, cwd=cwd, stdout=output, stderr=subprocess.STDOUT,
                                    timeout=timeout, creationflags=CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired:
        print(f"{name}: timed out after {timeout}s; child stopped; partial log: {log}")
        raise SystemExit(1) from None
    text = log.read_text(encoding="utf-8", errors="replace")
    print(f"{name}: exit={result.returncode}; {log}")
    if result.returncode:
        print(text[-8000:])
        raise SystemExit(1)
    return text


def run_godot(engine: Path, name: str, arguments: list[str], marker: str | None = None, timeout: int = 180) -> None:
    log = REPORT / f"build-{name}.log"
    engine_log = REPORT / f"build-{name}-engine.log"
    command = [str(engine), "--headless", "--path", str(PROJECT), "--log-file", str(engine_log.resolve()), *arguments]
    try:
        with log.open("w", encoding="utf-8") as output:
            result = subprocess.run(command, cwd=PROJECT, stdout=output, stderr=subprocess.STDOUT,
                                    timeout=timeout, creationflags=CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired:
        print(f"{name}: timed out after {timeout}s; child stopped; partial log: {log}")
        raise SystemExit(1) from None
    text = log.read_text(encoding="utf-8", errors="replace")
    clean = re.sub(r"\x1b\[[0-9;]*m", "", text)
    errors = [line for line in clean.splitlines() if line.lstrip().startswith(("ERROR:", "SCRIPT ERROR:"))]
    print(f"{name}: exit={result.returncode}, errors={len(errors)}; {log}")
    if result.returncode or errors or (marker and marker not in clean):
        print(text[-8000:])
        raise SystemExit(1)


def copy_documents_and_licenses() -> None:
    for name in ("README-stage1.md", "THIRD_PARTY_NOTICES.md"):
        shutil.copy2(ROOT / name, OUTPUT / name)
    licenses = OUTPUT / "licenses"
    licenses.mkdir(exist_ok=True)
    for source in (PROJECT / "addons").rglob("*LICENSE*"):
        if source.is_file():
            label = "-".join(source.relative_to(PROJECT / "addons").parts)
            shutil.copy2(source, licenses / label)
    shutil.copy2(PROJECT / "models" / "ATTRIBUTION.txt", licenses / "assets-ATTRIBUTION.txt")


def write_manifest() -> None:
    files = []
    for path in sorted(OUTPUT.rglob("*")):
        if path.is_file() and path.name != "manifest.json":
            files.append({
                "path": path.relative_to(OUTPUT).as_posix(),
                "size": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            })
    (OUTPUT / "manifest.json").write_text(json.dumps(files, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", type=Path,
                        default=Path(os.environ.get("PET3D_ENGINE") or str(DEFAULT_ENGINE)))
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    if os.name != "nt":
        raise SystemExit("Stage 1 package is Windows-only")
    if not args.engine.is_file():
        raise SystemExit(f"Godot engine not found: {args.engine}")
    REPORT.mkdir(parents=True, exist_ok=True)

    run_command("native-compile", [str(ROOT / "native" / "build.cmd"), "--stage1"], 240)
    run_command("native-tests", [sys.executable, "-B", str(ROOT / "tests" / "native" / "run_tests.py"), "--stage1"], 180)
    run_command("service-source-tests", [sys.executable, "-B", "-m", "pytest", "tests/service", "-q",
                                                "-p", "no:cacheprovider", "--basetemp", "tests/service/.tmp-stage1-source"], 180)
    run_godot(args.engine, "import", ["--editor", "--import"], timeout=240)
    run_godot(args.engine, "protocol-tests", ["--script", "res://tests/test_protocol.gd"], "PROTOCOL_TESTS checks=7 failures=0")
    run_godot(args.engine, "settings-tests", ["--script", "res://tests/test_app_settings.gd"], "APP_SETTINGS_TESTS checks=8 failures=0")
    run_godot(args.engine, "window-tests", ["--script", "res://tests/test_windows.gd"], "WINDOW_TESTS checks=8 failures=0")
    run_godot(args.engine, "service-client-tests", ["--script", "res://tests/test_service_client.gd"], "SERVICE_CLIENT_TESTS checks=10 failures=0", 120)
    if args.check_only:
        return

    safe_clean_output(OUTPUT)
    run_command("service-build", [sys.executable, "-B", str(ROOT / "tools" / "build_service.py")], 300)
    run_command("service-packaged-tests", [sys.executable, "-B", "-m", "pytest", "tests/service", "-q",
        "-p", "no:cacheprovider", "--basetemp", "tests/service/.tmp-stage1-packaged",
        "--service-exe", str(OUTPUT / "service" / "pet-service.exe")], 180)
    run_godot(args.engine, "export-release", ["--export-release", "Windows", str(OUTPUT / "Pet3D.exe")], timeout=360)
    copy_documents_and_licenses()
    write_manifest()
    verify_package(OUTPUT, REPORT)
    print(f"Stage 1 exported and verified: {OUTPUT}")


if __name__ == "__main__":
    main()
