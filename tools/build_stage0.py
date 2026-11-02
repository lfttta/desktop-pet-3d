"""Reproducible Godot import/test/Windows export; never touches source references."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess

from verify_stage0_package import verify_package

ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT / "prototypes" / "stage0"
REPORT = ROOT / "docs" / "reports" / "stage-0"
DEFAULT_ENGINE = Path(r"D:\danjigame\GODDOT  WORD\Godot_v4.7.2-stable_win64.exe")


def run(engine: Path, name: str, args: list[str], timeout: int = 180, *, report_dir: Path = REPORT) -> None:
    log = report_dir / (name + ".log")
    command = [str(engine), "--headless", "--path", str(PROJECT), "--log-file",
               str(report_dir / (name + "-engine.log")), *args]
    with log.open("w", encoding="utf-8") as output:
        result = subprocess.run(command, cwd=PROJECT, stdout=output, stderr=subprocess.STDOUT,
                                timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    text = log.read_text(encoding="utf-8", errors="replace")
    clean = re.sub(r"\x1b\[[0-9;]*m", "", text)
    errors = [line for line in clean.splitlines() if line.lstrip().startswith(("ERROR:", "SCRIPT ERROR:"))]
    print(f"{name}: exit={result.returncode}, errors={len(errors)}; {log}")
    if result.returncode or errors:
        print(text[-8000:])
        raise SystemExit(1)
    markers = {"interaction-tests": "INTERACTION_TESTS", "character-tests": "CHARACTER_TESTS", "parent-tests": "PARENT_TESTS", "placement-tests": "PLACEMENT_TESTS"}
    if name in markers and not re.search(rf"^{markers[name]} checks=[1-9]\d* failures=0$", clean, re.MULTILINE):
        raise SystemExit(f"{name} did not report success")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine", type=Path, default=DEFAULT_ENGINE)
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--output", type=Path, help="Isolated package output directory (default: dist/stage-0)")
    parser.add_argument("--report-dir", type=Path, help="Override build/test log directory")
    args = parser.parse_args()
    report = (args.report_dir or REPORT).resolve()
    report.mkdir(parents=True, exist_ok=True)
    if not args.engine.is_file():
        raise SystemExit(f"Godot engine not found: {args.engine}")
    run(args.engine, "import", ["--editor", "--import"], report_dir=report)
    run(args.engine, "interaction-tests", ["--script", "res://tests/test_interaction.gd"], report_dir=report)
    run(args.engine, "character-tests", ["--script", "res://tests/test_characters.gd"], report_dir=report)
    run(args.engine, "secondary-tests", ["--script", "res://tests/test_secondary.gd"], report_dir=report)
    run(args.engine, "parent-tests", ["--script", "res://tests/test_parent.gd"], report_dir=report)
    run(args.engine, "placement-tests", ["--script", "res://tests/test_placement.gd"], report_dir=report)
    if args.check_only:
        return
    output = (args.output or ROOT / "dist" / "stage-0").resolve()
    output.mkdir(parents=True, exist_ok=True)
    run(args.engine, "export-release", ["--export-release", "Windows", str(output / "Pet3D-Stage0.exe")], 300, report_dir=report)
    for name in ("README-stage0.md", "THIRD_PARTY_NOTICES.md"):
        source = ROOT / name
        if source.exists():
            shutil.copy2(source, output / name)
    licenses = output / "licenses"
    licenses.mkdir(exist_ok=True)
    for source in (PROJECT / "addons").rglob("*LICENSE*"):
        if source.is_file():
            label = "-".join(source.relative_to(PROJECT / "addons").parts)
            shutil.copy2(source, licenses / label)
    shutil.copy2(PROJECT / "models" / "ATTRIBUTION.txt", licenses / "assets-ATTRIBUTION.txt")
    verify_package(output, report)
    files = []
    for path in sorted(output.rglob("*")):
        if path.is_file() and path.name != "manifest.json":
            files.append({"path": path.relative_to(output).as_posix(), "size": path.stat().st_size,
                          "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    (output / "manifest.json").write_text(json.dumps(files, indent=2), encoding="utf-8")
    print(f"Exported {len(files)} files to {output}")


if __name__ == "__main__":
    main()
