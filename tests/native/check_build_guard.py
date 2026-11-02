"""Verify the no-argument native build cannot publish over preserved artifacts."""
from pathlib import Path
import hashlib
import json
import subprocess

ROOT = Path(__file__).resolve().parents[2]
RESULTS = Path(__file__).resolve().parent / "results"


def snapshot():
    result = {}
    for folder in ("prototypes/stage0", "dist/stage-0", "app/addons/pet_window"):
        for path in (ROOT / folder).rglob("*"):
            if path.is_file():
                stat = path.stat()
                with path.open("rb") as stream:
                    digest = hashlib.file_digest(stream, "sha256").hexdigest()
                result[str(path.relative_to(ROOT))] = [stat.st_size, stat.st_mtime_ns, digest]
    return result


before = snapshot()
build = subprocess.run(["cmd.exe", "/d", "/c", str(ROOT / "native/build.cmd")],
                       cwd=ROOT, capture_output=True, timeout=120)
RESULTS.mkdir(exist_ok=True)
(RESULTS / "build-default.log").write_bytes(build.stdout + build.stderr)
assert build.returncode == 0, f"Default build failed: see {RESULTS / 'build-default.log'}"
after = snapshot()
changed = sorted(key for key in before.keys() | after.keys() if before.get(key) != after.get(key))
result = {"default_build_exit": build.returncode, "protected_files": len(before), "changed": changed}
(RESULTS / "build-guard.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
assert not changed, f"Default build changed protected artifacts: {changed}"
print(f"PASS: default build preserved content and timestamps of {len(before)} stage0/app addon files")
