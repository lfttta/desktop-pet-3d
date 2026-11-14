"""Isolated native/API checks; does not open or import the shared prototype."""
from pathlib import Path
import argparse
import json
import shutil
import subprocess
import hashlib
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
parser = argparse.ArgumentParser()
parser.add_argument("--stage1", action="store_true", help="explicit alias for the default stage1 test suite")
parser.add_argument("--godot", default=r"D:\danjigame\GODDOT  WORD\Godot_v4.7.2-stable_win64.exe")
args = parser.parse_args()
logs = HERE / "results"
logs.mkdir(exist_ok=True)
(logs / "summary.json").unlink(missing_ok=True)

def run(name: str, command: list[str], marker: str | None = None) -> None:
    try:
        result = subprocess.run(command, cwd=HERE, capture_output=True, timeout=30)
    except subprocess.TimeoutExpired as error:
        output = ((error.stdout or b"") + (error.stderr or b"")).decode("utf-8", errors="replace")
        (logs / (name + ".log")).write_text(output, encoding="utf-8")
        raise SystemExit(f"{name} timed out; child stopped, diagnostic log: {logs / (name + '.log')}")
    output = (result.stdout + result.stderr).decode("utf-8", errors="replace")
    (logs / (name + ".log")).write_text(output, encoding="utf-8")
    print(output, end="")
    if result.returncode or "SCRIPT ERROR:" in output or "ERROR:" in output or "FAIL:" in output:
        raise SystemExit(f"{name} failed (exit {result.returncode}); see {logs}")
    if marker and marker not in output:
        raise SystemExit(f"{name}: expected success marker missing")

addon = ROOT / "app/addons/pet_window"
target = HERE / "godot/addons/pet_window"
target.mkdir(parents=True, exist_ok=True)
shutil.copy2(addon / "pet_window.gdextension", target)
(target / "bin").mkdir(exist_ok=True)
for binary in (addon / "bin").glob("*.dll"):
    shutil.copy2(binary, target / "bin" / binary.name)
# Register only this extension; no reliance on a GUI editor or project import.
cache = HERE / "godot/.godot"
cache.mkdir(exist_ok=True)
(cache / "extension_list.cfg").write_text("res://addons/pet_window/pet_window.gdextension\n", encoding="utf-8")

run("win32", [str(ROOT / "native/build/window_bridge_test.exe")], "hidden-HWND native assertions")
run("lifetime", [str(ROOT / "native/build/lifetime_test.exe")], "lifetime/hotkey assertions, 0 failures")
run("stage1", [str(ROOT / "native/build/stage1_test.exe")], "stage1 subprocess/resource assertions")
run("godot_api", [args.godot, "--headless", "--path", str(HERE / "godot"), "--script", "res://smoke.gd"],
    "PASS: seventeen-method API")
config = target / "pet_window.gdextension"
original_config = config.read_text(encoding="utf-8")
try:
    # The editor selects debug features. Point that entry at the delivered
    # release file to check its actual load path without exporting the app.
    config.write_text(original_config.replace("pet_window.windows.debug.x86_64.dll",
        "pet_window.windows.release.x86_64.dll"), encoding="utf-8")
    run("godot_release_api", [args.godot, "--headless", "--path", str(HERE / "godot"),
        "--script", "res://smoke.gd"], "PASS: seventeen-method API")
finally:
    config.write_text(original_config, encoding="utf-8")
digests = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (addon / "bin").glob("*.dll")}
(logs / "summary.json").write_text(json.dumps({"native": "pass", "lifetime": "pass", "godot_api": "pass",
    "godot_release_api": "pass", "dll_sha256": digests,
    "engine": args.godot, "stage1": "pass", "real_desktop_ui": "not run; assigned to parent"}, indent=2), encoding="utf-8")
print("PASS: isolated native and headless engine checks")
