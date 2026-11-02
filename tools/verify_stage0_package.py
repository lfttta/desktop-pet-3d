"""Verify the generated Release EXE/PCK/DLLs, also from a Chinese/space path."""
from __future__ import annotations

import argparse
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "dist" / "stage-0"
REPORT = ROOT / "docs" / "reports" / "stage-0"


def verify(directory: Path, label: str, report_dir: Path = REPORT) -> None:
    directory = directory.resolve()
    for name in ("Pet3D-Stage0.exe", "Pet3D-Stage0.pck"):
        if not (directory / name).is_file():
            raise SystemExit(f"Package is missing {directory / name}")
    report_dir.mkdir(parents=True, exist_ok=True)
    log = report_dir / (label + ".log")
    # Official Release templates omit --script and disable --path overrides.
    # The runtime's headless verification entry loads the authoritative script;
    # its res:// resources come from the EXE's adjacent PCK, never PROJECT.
    command = [str(directory / "Pet3D-Stage0.exe"), "--headless", "--log-file",
               str((report_dir / (label + "-engine.log")).resolve()), "--",
               "--verify-package=" + str(ROOT / "tests" / "verify_export.gd")]
    try:
        with log.open("w", encoding="utf-8") as output:
            process = subprocess.run(command, cwd=directory, stdout=output, stderr=subprocess.STDOUT,
                                     timeout=60, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired) as error:
        raise SystemExit(f"{label}: {error}; {log}") from error
    text = log.read_text(encoding="utf-8", errors="replace")
    clean = re.sub(r"\x1b\[[0-9;]*m", "", text)
    failed = process.returncode != 0 or "EXPORT_TEST failures=0" not in clean
    failed |= any(line.lstrip().startswith(("ERROR:", "SCRIPT ERROR:")) for line in clean.splitlines())
    print(f"{label}: {'FAIL' if failed else 'PASS'}, exit={process.returncode}; {log}")
    if failed:
        print(text[-5000:])
        raise SystemExit(1)


def verify_package(directory: Path, report_dir: Path = REPORT) -> None:
    verify(directory, "export-package-tests", report_dir)
    # One fresh copy avoids stale artifacts and avoids keeping a second package.
    with tempfile.TemporaryDirectory(prefix="Pet3D-中文 路径-") as temporary:
        chinese = Path(temporary) / "阶段 0"
        shutil.copytree(directory, chinese)
        verify(chinese, "export-chinese-path-tests", report_dir)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, default=SOURCE)
    parser.add_argument("--report-dir", type=Path, default=REPORT)
    args = parser.parse_args()
    verify_package(args.package.resolve(), args.report_dir.resolve())


if __name__ == "__main__":
    main()
