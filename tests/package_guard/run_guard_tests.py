"""Exercise the production package guard using a separate, tiny Windows export."""
from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ENGINE = Path(r"D:\danjigame\GODDOT  WORD\Godot_v4.7.2-stable_win64.exe")


def run(command: list[str], cwd: Path, log: Path, expected: int = 0) -> str:
    with log.open("w", encoding="utf-8") as output:
        result = subprocess.run(command, cwd=cwd, stdout=output, stderr=subprocess.STDOUT,
                                timeout=120, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    text = log.read_text(encoding="utf-8", errors="replace")
    if result.returncode != expected or "SCRIPT ERROR:" in text or (expected == 0 and "ERROR:" in text):
        raise SystemExit(f"{log.name}: unexpected exit {result.returncode}\n{text[-6000:]}")
    return text


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", type=Path, default=DEFAULT_ENGINE)
    parser.add_argument("--report-dir", type=Path,
                        default=ROOT / "prototypes/stage0/tests/results/app-fixes/package-guard")
    args = parser.parse_args()
    report = args.report_dir.resolve()
    report.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="stage0-package-guard-") as temporary:
        base = Path(temporary)
        project = base / "source"
        package = base / "package"
        project.mkdir()
        package.mkdir()
        (project / "project.godot").write_text('''config_version=5
[application]
config/name="Stage0 package guard fixture"
run/main_scene="res://main.tscn"
[rendering]
renderer/rendering_method="gl_compatibility"
''', encoding="utf-8")
        (project / "main.tscn").write_text('[gd_scene load_steps=2 format=3]\n[ext_resource type="Script" path="res://runner.gd" id="1"]\n[node name="Fixture" type="Node"]\nscript = ExtResource("1")\n', encoding="utf-8")
        (project / "runner.gd").write_text('extends Node\nfunc _ready() -> void:\n\tget_tree().quit(preload("res://guard_suite.gd").new().run())\n', encoding="utf-8")
        (project / "forbidden.gd").write_text('extends RefCounted\n', encoding="utf-8")
        (project / "private.json").write_text('{"fixture": true}\n', encoding="utf-8")
        (project / "pixel.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg" width="2" height="2"><rect width="2" height="2" fill="red"/></svg>', encoding="utf-8")
        # The production guard is copied at execution time, never maintained twice.
        shutil.copy2(ROOT / "tests/verify_export.gd", project / "verifier.gd")
        shutil.copy2(ROOT / "tests/package_guard/verify_guard.gd", project / "guard_suite.gd")
        (project / "export_presets.cfg").write_text('''[preset.0]
name="Windows"
platform="Windows Desktop"
runnable=true
export_filter="all_resources"
include_filter="*.json"
exclude_filter=""
script_export_mode=2
[preset.0.options]
binary_format/embed_pck=false
binary_format/architecture="x86_64"
debug/export_console_wrapper=0
application/modify_resources=false
''', encoding="utf-8")
        editor = [str(args.engine.resolve()), "--headless", "--path", str(project)]
        run([*editor, "--editor", "--import"], project, report / "import.log")
        executable = package / "Fixture.exe"
        run([*editor, "--export-release", "Windows", str(executable)], project, report / "export.log")
        # Official release templates disable --path/--main-pack overrides.
        # Launch from the isolated package directory and use the adjacent PCK.
        command = [str(executable), "--headless"]
        positive = run(command, package, report / "guard.log")
        if "PACKAGE_GUARD_TEST checks=9 failures=0" not in positive:
            raise SystemExit("Package guard did not complete all nine checks")
        negative = run([*command, "--", "--reject-fixtures"], package, report / "negative.log", expected=1)
        if "NEGATIVE_EXPORT rejected=3 checks_failed=0" not in negative:
            raise SystemExit("Negative export did not reject all three included fixtures")
    print(f"PACKAGE_GUARD PASS: 9 checks; separate negative export rejected 3 fixtures; {report}")


if __name__ == "__main__":
    main()
