"""Build only the stage-1 service; never touch the Godot/native release files.

Run: & 'D:/phthon lang/python.exe' -B tools/build_service.py
"""

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'dist/stage-1/service')
    parser.add_argument('--source-root', type=Path, default=ROOT)
    args = parser.parse_args()
    if os.name != 'nt' or sys.version_info[:3] != (3, 13, 14):
        raise SystemExit('Use Windows Python 3.13.14: D:/phthon lang/python.exe')
    source_root = args.source_root.resolve()
    if not (source_root / 'service/main.py').is_file():
        raise SystemExit('Service source is missing')
    work = source_root / 'build/service'
    destination = args.output.resolve()
    # Resolve before allowing build tools/copy operations to write to these trees.
    for path in (work, destination):
        if path.is_symlink():
            raise SystemExit('Refusing redirected build/output directory')
        path.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment['PYTHONDONTWRITEBYTECODE'] = '1'
    environment['PYINSTALLER_CONFIG_DIR'] = str(work / 'cache')
    environment['PYTHONPYCACHEPREFIX'] = str(work / 'pycache')
    for variable in ('TEMP', 'TMP'):
        environment[variable] = str(work / 'tmp')
    (work / 'tmp').mkdir(exist_ok=True)
    # The persona card and the phrase library are read at runtime, not imported,
    # so PyInstaller does not collect them. Without this the packaged service
    # falls back to the built-in persona and logs "Persona card godette is
    # missing or invalid" on every start.
    data_root = source_root / 'data'
    if not (data_root / 'persona/godette.json').is_file():
        raise SystemExit('Service data is missing: data/persona/godette.json')
    command = [
        sys.executable, '-B', '-m', 'PyInstaller', '--noconfirm', '--clean',
        '--onedir', '--console', '--noupx', '--name', 'pet-service',
        '--distpath', str(work / 'bundle'), '--workpath', str(work / 'work'),
        '--specpath', str(work), '--paths', str(source_root),
        '--add-data', str(data_root) + os.pathsep + 'data',
        str(source_root / 'service/main.py'),
    ]
    subprocess.run(command, cwd=source_root, env=environment, check=True)
    bundle = work / 'bundle/pet-service'
    if not (bundle / 'pet-service.exe').is_file():
        raise SystemExit('PyInstaller did not produce pet-service.exe')
    if not (bundle / '_internal/data/persona/godette.json').is_file():
        raise SystemExit('PyInstaller did not bundle data/persona')
    shutil.copytree(bundle, destination, dirs_exist_ok=True)
    print(destination / 'pet-service.exe')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
