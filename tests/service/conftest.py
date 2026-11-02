import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def pytest_addoption(parser):
    parser.addoption('--service-exe', default=None,
                     help='Run pipe contracts against a packaged service executable')
    parser.addoption('--build-service', action='store_true', default=False,
                     help='Build and verify PyInstaller release (Windows/Python 3.13.14)')
