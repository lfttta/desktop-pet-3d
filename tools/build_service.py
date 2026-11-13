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


def check_python(os_name: str, version: tuple) -> str:
    """Require Windows Python 3.13; warn on a different patch level."""
    if os_name != 'nt' or tuple(version[:2]) != (3, 13):
        raise SystemExit('Use Windows Python 3.13: D:/phthon lang/python.exe')
    if tuple(version[:3]) != (3, 13, 14):
        return ('WARN: Python ' + '.'.join(map(str, version[:3])) +
                ' differs from the validated 3.13.14 patch level')
    return ''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'dist/stage-1/service')
    parser.add_argument('--source-root', type=Path, default=ROOT)
    args = parser.parse_args()
    warning = check_python(os.name, sys.version_info[:3])
    if warning:
        print(warning)
    source_root = args.source_root.resolve()
    if not (source_root / 'service/main.py').is_file():
        raise SystemExit('Service source is missing')
    work = source_root / 'build/service'
    destination = args.output.resolve()
    # Resolve before allowing build tools/copy operations to write to these trees.
    for path in (work, destination):
        if path.is_symlink():
            raise SystemExit('Refusing redirected build/output directory')
    # The staging dir is cleaned before copying; never allow it to overlap the
    # source tree or the PyInstaller work area that holds the fresh bundle.
    service_source = source_root / 'service'
    if (destination == service_source or destination.is_relative_to(service_source)
            or destination in service_source.parents
            or destination == work or destination in work.parents):
        raise SystemExit('Refusing to clean an output directory that overlaps source or build files')
    for path in (work, destination):
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
    # Clean the staging dir so standalone runs never keep stale files from a
    # previous build. Only the dedicated output dir is removed, never source.
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(bundle, destination)
    print(destination / 'pet-service.exe')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
