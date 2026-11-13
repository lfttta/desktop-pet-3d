"""Focused checks for the hardened build tools: path overrides, timeout
cleanup, line-start ERROR detection, Python gate and staging hygiene.

Runs on any OS; Windows-only steps (PyInstaller, Godot) are mocked. The PIL,
ctypes and bpy stubs below are in-memory test doubles, never repository files.
"""
from __future__ import annotations

import ctypes
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))


class _Win32Stub:
    """Absorbs module-level ``WinDLL``/``argtypes`` setup on non-Windows hosts.

    ``tools/build_stage2.py`` imports ``verify_godette_desktop`` at module level,
    which binds user32/kernel32/psapi as soon as it is imported. Nothing in these
    tests calls those functions, so a no-op object is enough to let the build
    modules import off Windows.
    """

    def __getattr__(self, name):
        return _Win32Stub()

    def __call__(self, *args, **kwargs):
        return _Win32Stub()

    def __setattr__(self, name, value):
        pass


if not hasattr(ctypes, "WinDLL"):  # Real Win32 hosts keep the genuine bindings.
    ctypes.WinDLL = lambda *args, **kwargs: _Win32Stub()
    ctypes.WINFUNCTYPE = lambda *args, **kwargs: (lambda function: function)

try:  # verify_recovery_visuals imports PIL at module level.
    import PIL  # noqa: F401
except ImportError:
    _pil = types.ModuleType("PIL")
    _pil.Image = types.ModuleType("PIL.Image")
    _pil.ImageChops = types.ModuleType("PIL.ImageChops")
    sys.modules["PIL"] = _pil
    sys.modules["PIL.Image"] = _pil.Image
    sys.modules["PIL.ImageChops"] = _pil.ImageChops

import build_stage1 as stage1  # noqa: E402
import build_stage2 as stage2  # noqa: E402
import build_service as service  # noqa: E402


def _completed(returncode: int = 0):
    return types.SimpleNamespace(returncode=returncode)


class RunErrorDetectionTests(unittest.TestCase):
    """build_stage2.run must catch an ERROR on ANY line, including the first."""

    def setUp(self) -> None:
        import tempfile
        self._tmp = tempfile.TemporaryDirectory(prefix="stage2-run-")
        self.addCleanup(self._tmp.cleanup)
        self.report = Path(self._tmp.name)
        patcher = patch.object(stage2, "REPORT", self.report)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run_with_output(self, text: str, returncode: int = 0, marker: str = ""):
        def fake_run(command, **kwargs):
            kwargs["stdout"].write(text)
            return _completed(returncode)
        with patch.object(stage2.subprocess, "run", side_effect=fake_run):
            return stage2.run("fixture", ["fixture"], marker)

    def test_error_on_first_line_is_detected(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "fixture failed"):
            self._run_with_output("ERROR: boom on the first line\nrest\n")

    def test_error_mid_log_is_detected(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "fixture failed"):
            self._run_with_output("ok\n  ERROR: indented boom\n")

    def test_script_error_is_detected(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "fixture failed"):
            self._run_with_output("SCRIPT ERROR: parse failure\n")

    def test_clean_log_with_marker_passes(self) -> None:
        text = self._run_with_output("all good\nMARKER ok\n", marker="MARKER ok")
        self.assertIn("all good", text)

    def test_missing_marker_fails(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "fixture failed"):
            self._run_with_output("all good\n", marker="MARKER ok")

    def test_timeout_raises_clear_error(self) -> None:
        def fake_run(command, **kwargs):
            raise subprocess.TimeoutExpired(command, 1)
        with patch.object(stage2.subprocess, "run", side_effect=fake_run):
            with self.assertRaisesRegex(RuntimeError, "timed out after 1s"):
                stage2.run("fixture", ["fixture"], timeout=1)


class Stage1TimeoutTests(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile
        self._tmp = tempfile.TemporaryDirectory(prefix="stage1-run-")
        self.addCleanup(self._tmp.cleanup)
        patcher = patch.object(stage1, "REPORT", Path(self._tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_run_command_timeout_exits_cleanly(self) -> None:
        def fake_run(command, **kwargs):
            raise subprocess.TimeoutExpired(command, 1)
        with patch.object(stage1.subprocess, "run", side_effect=fake_run):
            with self.assertRaises(SystemExit) as caught:
                stage1.run_command("fixture", ["fixture"], timeout=1)
        self.assertEqual(caught.exception.code, 1)

    def test_run_godot_timeout_exits_cleanly(self) -> None:
        def fake_run(command, **kwargs):
            raise subprocess.TimeoutExpired(command, 1)
        with patch.object(stage1.subprocess, "run", side_effect=fake_run):
            with self.assertRaises(SystemExit) as caught:
                stage1.run_godot(Path("engine.exe"), "fixture", [], timeout=1)
        self.assertEqual(caught.exception.code, 1)


class PathOverrideTests(unittest.TestCase):
    def test_cli_wins_over_env_and_default(self) -> None:
        with patch.dict(os.environ, {"PET3D_ENGINE": "/env/engine"}):
            self.assertEqual(stage2.resolve_path(Path("/cli/engine"), "PET3D_ENGINE", Path("/default")),
                             Path("/cli/engine"))

    def test_env_wins_over_default(self) -> None:
        with patch.dict(os.environ, {"PET3D_ENGINE": "/env/engine"}):
            self.assertEqual(stage2.resolve_path(None, "PET3D_ENGINE", Path("/default")),
                             Path("/env/engine"))

    def test_default_when_unset(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(stage2.resolve_path(None, "PET3D_ENGINE", Path("/default")),
                             Path("/default"))

    def test_empty_env_falls_back_to_default(self) -> None:
        with patch.dict(os.environ, {"PET3D_ENGINE": ""}):
            self.assertEqual(stage2.resolve_path(None, "PET3D_ENGINE", Path("/default")),
                             Path("/default"))


class PythonGateTests(unittest.TestCase):
    def test_stage2_accepts_validated_patch(self) -> None:
        self.assertEqual(stage2.check_python((3, 13, 14), True), "")

    def test_stage2_warns_on_other_patch(self) -> None:
        self.assertIn("WARN", stage2.check_python((3, 13, 0), True))

    def test_stage2_rejects_other_minor(self) -> None:
        with self.assertRaises(SystemExit):
            stage2.check_python((3, 12, 3), True)

    def test_stage2_rejects_missing_venv(self) -> None:
        with self.assertRaises(SystemExit):
            stage2.check_python((3, 13, 14), False)

    def test_service_accepts_validated_patch(self) -> None:
        self.assertEqual(service.check_python("nt", (3, 13, 14)), "")

    def test_service_warns_on_other_patch(self) -> None:
        self.assertIn("WARN", service.check_python("nt", (3, 13, 2)))

    def test_service_rejects_non_windows(self) -> None:
        with self.assertRaises(SystemExit):
            service.check_python("posix", (3, 13, 14))

    def test_service_rejects_other_minor(self) -> None:
        with self.assertRaises(SystemExit):
            service.check_python("nt", (3, 14, 0))


class CandidateCleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile
        self._tmp = tempfile.TemporaryDirectory(prefix="stage2-candidate-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        patcher = patch.object(stage2, "ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_removes_only_this_runs_candidate(self) -> None:
        base = self.root / "build/stage2"
        candidate = base / "package-20260101-000000"
        (candidate / "service").mkdir(parents=True)
        sibling = base / "package-20250101-000000"
        sibling.mkdir()
        stage2.cleanup_candidate(candidate)
        self.assertFalse(candidate.exists())
        self.assertTrue(sibling.is_dir())

    def test_refuses_directory_outside_build_area(self) -> None:
        outside = self.root / "dist" / "package-20260101-000000"
        outside.mkdir(parents=True)
        stage2.cleanup_candidate(outside)
        self.assertTrue(outside.is_dir())

    def test_refuses_non_candidate_name(self) -> None:
        base = self.root / "build/stage2"
        other = base / "reports"
        other.mkdir(parents=True)
        stage2.cleanup_candidate(other)
        self.assertTrue(other.is_dir())


class ServiceStagingHygieneTests(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile
        self._tmp = tempfile.TemporaryDirectory(prefix="service-staging-")
        self.addCleanup(self._tmp.cleanup)
        self.source = Path(self._tmp.name) / "repo"
        (self.source / "service").mkdir(parents=True)
        (self.source / "service/main.py").write_text("# fixture\n", encoding="utf-8")

    def _run_main(self, output: Path):
        def fake_run(command, **kwargs):
            bundle = self.source / "build/service/bundle/pet-service"
            bundle.mkdir(parents=True, exist_ok=True)
            (bundle / "pet-service.exe").write_bytes(b"MZ")
            (bundle / "fresh.dll").write_bytes(b"new")
            return _completed()
        argv = ["build_service.py", "--source-root", str(self.source), "--output", str(output)]
        with patch.object(service, "check_python", return_value=""), \
             patch.object(service.subprocess, "run", side_effect=fake_run), \
             patch.object(sys, "argv", argv):
            return service.main()

    def test_stale_files_are_removed_before_copy(self) -> None:
        output = self.source / "dist/stage-1/service"
        output.mkdir(parents=True)
        (output / "stale.txt").write_text("old", encoding="utf-8")
        self.assertEqual(self._run_main(output), 0)
        self.assertFalse((output / "stale.txt").exists())
        self.assertTrue((output / "pet-service.exe").is_file())
        self.assertTrue((output / "fresh.dll").is_file())

    def test_refuses_output_overlapping_source(self) -> None:
        with self.assertRaisesRegex(SystemExit, "overlaps"):
            self._run_main(self.source / "service")

    def test_refuses_output_at_source_root(self) -> None:
        with self.assertRaisesRegex(SystemExit, "overlaps"):
            self._run_main(self.source)

    def test_refuses_output_at_work_area(self) -> None:
        with self.assertRaisesRegex(SystemExit, "overlaps"):
            self._run_main(self.source / "build/service")


class ConvertStage2PathTests(unittest.TestCase):
    """convert_stage2 runs under Blender; import it with in-memory bpy doubles."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._saved = {name: sys.modules.get(name) for name in ("bpy", "mathutils", "convert_stage2")}
        bpy = types.ModuleType("bpy")
        mathutils = types.ModuleType("mathutils")
        mathutils.Matrix = object
        mathutils.Quaternion = object
        mathutils.Vector = object
        sys.modules["bpy"] = bpy
        sys.modules["mathutils"] = mathutils
        spec = importlib.util.spec_from_file_location(
            "convert_stage2", ROOT / "tools/assets/convert_stage2.py")
        cls.module = importlib.util.module_from_spec(spec)
        sys.modules["convert_stage2"] = cls.module
        spec.loader.exec_module(cls.module)

    @classmethod
    def tearDownClass(cls) -> None:
        for name, module in cls._saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module

    def test_defaults_match_project_layout(self) -> None:
        root, source = self.module.resolve_paths([], {})
        self.assertEqual(root, ROOT)
        self.assertEqual(source, self.module.DEFAULT_SOURCE_ROOT)

    def test_cli_overrides(self) -> None:
        root, source = self.module.resolve_paths(
            ["--repo-root", "/repo", "--fbx-source", "/fbx"], {})
        self.assertEqual(root, Path("/repo"))
        self.assertEqual(source, Path("/fbx"))

    def test_env_overrides(self) -> None:
        root, source = self.module.resolve_paths(
            [], {"PET3D_REPO_ROOT": "/envrepo", "PET3D_FBX_SOURCE": "/envfbx"})
        self.assertEqual(root, Path("/envrepo"))
        self.assertEqual(source, Path("/envfbx"))

    def test_cli_wins_over_env(self) -> None:
        root, source = self.module.resolve_paths(
            ["--fbx-source", "/cli"], {"PET3D_FBX_SOURCE": "/env"})
        self.assertEqual(source, Path("/cli"))
        self.assertEqual(root, ROOT)

    def test_unknown_blender_options_are_ignored(self) -> None:
        root, source = self.module.resolve_paths(
            ["--background", "--factory-startup", "--python-exit-code", "1",
             "--python", "tools/assets/convert_stage2.py", "--", "--fbx-source", "/fbx"], {})
        self.assertEqual(source, Path("/fbx"))
        self.assertEqual(root, ROOT)


if __name__ == "__main__":
    unittest.main()