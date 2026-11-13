"""The shipped Windows export must keep the action menu table in the package.

The native tray menu is fed from res://assets_data/action_menu.json; if the
export include_filter drops it, the packaged app silently loses its 65-action
menu. This guard reads the integrator-owned preset without modifying it.
"""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[2]
REQUIRED_JSON = (
    "assets_data/godette.json",
    "assets_data/characters.json",
    "assets_data/humanoid_core.json",
    "assets_data/action_menu.json",
)


class ExportFilterTests(unittest.TestCase):
    def test_include_filter_requires_action_menu_and_contract_json(self) -> None:
        text = (ROOT / "app/export_presets.cfg").read_text("utf-8")
        match = re.search(r'^include_filter="([^"]*)"', text, re.MULTILINE)
        self.assertIsNotNone(match, "export preset must declare include_filter")
        entries = [item.strip() for item in match.group(1).split(",")]
        for required in REQUIRED_JSON:
            self.assertIn(required, entries,
                          f"include_filter must keep {required} in the package")


if __name__ == "__main__":
    unittest.main()