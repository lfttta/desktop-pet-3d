"""Verify the complete stage-1 portable package and its process lifecycle."""
from __future__ import annotations

import argparse
import ctypes
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "dist" / "stage-1"
REPORT = ROOT / "docs" / "reports" / "stage-1"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
STILL_ACTIVE = 259


def process_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        return bool(ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == STILL_ACTIVE
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def terminate_pid(pid: int) -> None:
    handle = ctypes.windll.kernel32.OpenProcess(0x0001 | 0x100000, False, pid)
    if not handle:
        raise RuntimeError(f"Could not open process {pid} for termination")
    try:
        if not ctypes.windll.kernel32.TerminateProcess(handle, 91):
            raise ctypes.WinError()
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def wait_pid_gone(pid: int, timeout: float = 8.0) -> None:
    deadline = time.monotonic() + timeout
    while process_is_running(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    if process_is_running(pid):
        raise AssertionError(f"Process {pid} did not exit within {timeout:.1f}s")


def read_report(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def wait_report(path: Path, predicate, timeout: float = 12.0) -> dict:
    deadline = time.monotonic() + timeout
    latest: dict = {}
    while time.monotonic() < deadline:
        latest = read_report(path)
        if latest and predicate(latest):
            return latest
        time.sleep(0.05)
    raise AssertionError(f"Timed out waiting for report condition: {path}; latest={latest}")


def assert_clean_log(path: Path) -> None:
    text = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
    clean = re.sub(r"\x1b\[[0-9;]*m", "", text)
    errors = [line for line in clean.splitlines() if line.lstrip().startswith(("ERROR:", "SCRIPT ERROR:"))]
    if errors:
        raise AssertionError(f"Godot errors in {path}:\n" + "\n".join(errors[-12:]))


def required_files(package: Path) -> None:
    for relative in (
        "Pet3D.exe",
        "Pet3D.pck",
        "pet_window.windows.release.x86_64.dll",
        "libvrm_physics.windows.template_release.x86_64.dll",
        "service/pet-service.exe",
        "README-stage1.md",
        "THIRD_PARTY_NOTICES.md",
    ):
        if not (package / relative).is_file():
            raise AssertionError(f"Package is missing {relative}")


def export_resource_check(package: Path, label: str, report_dir: Path) -> None:
    log = report_dir / f"{label}.log"
    engine_log = report_dir / f"{label}-engine.log"
    command = [
        str(package / "Pet3D.exe"), "--headless", "--log-file", str(engine_log.resolve()), "--",
        "--verify-package=" + str((ROOT / "tests" / "verify_stage1_export.gd").resolve()),
    ]
    with log.open("w", encoding="utf-8") as output:
        result = subprocess.run(command, cwd=package, stdout=output, stderr=subprocess.STDOUT,
                                timeout=90, creationflags=CREATE_NO_WINDOW)
    text = log.read_text(encoding="utf-8", errors="replace")
    if result.returncode or "STAGE1_EXPORT_TEST failures=0" not in text:
        raise AssertionError(f"{label} failed with exit {result.returncode}; see {log}")
    assert_clean_log(log)


def service_exchange(package: Path, data_dir: Path, messages: list[dict]) -> list[dict]:
    environment = os.environ.copy()
    environment["PATH"] = str(Path(environment.get("SystemRoot", "C:/Windows")) / "System32")
    environment.pop("PYTHONHOME", None)
    environment.pop("PYTHONPATH", None)
    payload = "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in messages).encode("utf-8")
    result = subprocess.run(
        [str(package / "service" / "pet-service.exe"), "--data-dir", str(data_dir)],
        input=payload, capture_output=True, cwd=package, env=environment, timeout=20,
        creationflags=CREATE_NO_WINDOW,
    )
    if result.returncode:
        raise AssertionError(result.stderr.decode("utf-8", errors="replace"))
    return [json.loads(line) for line in result.stdout.decode("utf-8").splitlines()]


def launch_app(package: Path, data_dir: Path, log: Path, report: Path, auto_exit: float | None) -> subprocess.Popen:
    command = [str(package / "Pet3D.exe"), "--headless", "--log-file", str(log.resolve()), "--",
               "--data-dir=" + str(data_dir.resolve()), "--report=" + str(report.resolve())]
    if auto_exit is not None:
        command.append(f"--auto-exit={auto_exit}")
    return subprocess.Popen(command, cwd=package, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            creationflags=CREATE_NO_WINDOW)


def wait_app(process: subprocess.Popen, log: Path, timeout: float = 20.0) -> None:
    try:
        code = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
        raise AssertionError(f"Application timed out; see {log}")
    if code != 0:
        raise AssertionError(f"Application exited {code}; see {log}")
    assert_clean_log(log)


def verify_corrupt_recovery(package: Path, root: Path, report_dir: Path) -> None:
    data = root / "损坏配置 数据"
    data.mkdir(parents=True)
    (data / "settings.json").write_bytes(b'{"schema_version":1,"user_name":')
    report = root / "corrupt-report.json"
    log = report_dir / "package-corrupt-config.log"
    app = launch_app(package, data, log, report, 2.5)
    wait_app(app, log)
    result = read_report(report)
    if not result.get("warnings"):
        raise AssertionError("Corrupt settings did not produce a visible warning")
    if not list(data.glob("settings.json.broken-*")):
        raise AssertionError("Corrupt settings were not preserved as a backup")


def seed_settings(package: Path, data: Path) -> None:
    replies = service_exchange(package, data, [
        {"version": 1, "id": "hello", "type": "hello", "payload": {"protocol_version": 1}},
        {"version": 1, "id": "save", "type": "settings.update", "payload": {
            "user_name": "阶段一 中文", "character_id": "swordsman", "scale": 1.25,
            "placement": {"x": 0.25, "y": 0.75, "edge_x": 0, "edge_y": 0},
        }},
        {"version": 1, "id": "stop", "type": "shutdown", "payload": {}},
    ])
    if [(reply["id"], reply["type"]) for reply in replies] != [("hello", "ready"), ("save", "result"), ("stop", "result")]:
        raise AssertionError(f"Unexpected service replies: {replies}")


def verify_restore(package: Path, root: Path, report_dir: Path) -> None:
    data = root / "恢复设置 数据"
    seed_settings(package, data)
    report = root / "restore-report.json"
    log = report_dir / "package-restore.log"
    app = launch_app(package, data, log, report, 3.0)
    ready = wait_report(report, lambda value: value.get("service_state") == "ready")
    settings = ready.get("saved_settings", {})
    if settings.get("user_name") != "阶段一 中文" or settings.get("character_id") != "swordsman" or settings.get("scale") != 1.25:
        raise AssertionError(f"Saved settings were not restored: {settings}")
    child = int(ready.get("service_pid", 0))
    wait_app(app, log)
    wait_pid_gone(child)


def verify_single_instance(package: Path, root: Path, report_dir: Path) -> None:
    data = root / "单实例 数据"
    report = root / "single-report.json"
    first_log = report_dir / "package-single-primary.log"
    second_log = report_dir / "package-single-secondary.log"
    first = launch_app(package, data, first_log, report, 6.0)
    ready = wait_report(report, lambda value: value.get("service_state") == "ready")
    child = int(ready.get("service_pid", 0))
    second = launch_app(package, data, second_log, root / "unused-secondary.json", 1.0)
    wait_app(second, second_log, 8.0)
    text = second_log.read_text(encoding="utf-8", errors="replace")
    if "existing instance notified" not in text:
        raise AssertionError("Second launch did not notify the existing instance")
    if first.poll() is not None:
        raise AssertionError("Primary instance exited when the duplicate was launched")
    wait_app(first, first_log, 12.0)
    wait_pid_gone(child)


def verify_service_restart(package: Path, root: Path, report_dir: Path) -> None:
    data = root / "服务恢复 数据"
    report = root / "restart-report.json"
    log = report_dir / "package-service-restart.log"
    app = launch_app(package, data, log, report, 8.0)
    ready = wait_report(report, lambda value: value.get("service_state") == "ready")
    first_child = int(ready.get("service_pid", 0))
    terminate_pid(first_child)
    wait_pid_gone(first_child)
    recovered = wait_report(report, lambda value: value.get("service_state") == "ready" and int(value.get("service_pid", 0)) not in (0, first_child))
    second_child = int(recovered["service_pid"])
    wait_app(app, log, 15.0)
    wait_pid_gone(second_child)


def verify_parent_death(package: Path, root: Path, report_dir: Path) -> None:
    data = root / "父进程回收 数据"
    report = root / "parent-death-report.json"
    log = report_dir / "package-parent-death.log"
    app = launch_app(package, data, log, report, None)
    ready = wait_report(report, lambda value: value.get("service_state") == "ready")
    child = int(ready.get("service_pid", 0))
    app.kill()
    app.wait(timeout=5)
    wait_pid_gone(child)


def lifecycle_checks(package: Path, report_dir: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="Pet3D-stage1-data-") as temporary:
        root = Path(temporary)
        verify_corrupt_recovery(package, root, report_dir)
        verify_restore(package, root, report_dir)
        verify_single_instance(package, root, report_dir)
        verify_service_restart(package, root, report_dir)
        verify_parent_death(package, root, report_dir)


def verify_package(package: Path, report_dir: Path = REPORT) -> None:
    package = package.resolve()
    report_dir = report_dir.resolve()
    report_dir.mkdir(parents=True, exist_ok=True)
    required_files(package)
    export_resource_check(package, "stage1-export-package", report_dir)
    with tempfile.TemporaryDirectory(prefix="Pet3D-阶段一 中文 空格-") as temporary:
        copied = Path(temporary) / "便携程序"
        shutil.copytree(package, copied)
        required_files(copied)
        export_resource_check(copied, "stage1-export-chinese-path", report_dir)
        lifecycle_checks(copied, report_dir)
    (report_dir / "package-verification.json").write_text(json.dumps({
        "package": str(package),
        "resource_check": "pass",
        "chinese_space_path": "pass",
        "corrupt_config_backup": "pass",
        "settings_restore": "pass",
        "single_instance": "pass",
        "service_restart_once": "pass",
        "parent_death_service_cleanup": "pass",
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Stage 1 package verification passed: {package}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, default=SOURCE)
    parser.add_argument("--report-dir", type=Path, default=REPORT)
    args = parser.parse_args()
    verify_package(args.package, args.report_dir)


if __name__ == "__main__":
    main()
