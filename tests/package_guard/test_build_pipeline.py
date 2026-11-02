"""An editor export returning success must not certify a broken release package."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
import build_stage0 as build


class BuildPipelineTests(unittest.TestCase):
    def test_export_success_without_pck_is_rejected_before_manifest(self) -> None:
        with tempfile.TemporaryDirectory(prefix="stage0-incomplete-package-") as temporary:
            root = Path(temporary)
            project = root / "prototypes/stage0"
            (project / "models").mkdir(parents=True)
            (project / "models/ATTRIBUTION.txt").write_text("fixture attribution", encoding="utf-8")
            engine = root / "engine.exe"
            engine.write_bytes(b"fixture")

            # Only the external editor/export operation is replaced. The real
            # build's post-export validation and success/manifest policy run.
            def editor_export(_engine, name, args, *_positional, **_keywords):
                if name == "export-release":
                    Path(args[-1]).write_bytes(b"MZ")

            with patch.object(build, "ROOT", root), patch.object(build, "PROJECT", project), \
                 patch.object(build, "REPORT", root / "logs"), patch.object(build, "run", editor_export), \
                 patch.object(sys, "argv", ["build_stage0.py", "--engine", str(engine)]):
                with self.assertRaisesRegex(SystemExit, "Pet3D-Stage0.pck"):
                    build.main()
            self.assertFalse((root / "dist/stage-0/manifest.json").exists())


if __name__ == "__main__":
    unittest.main()
