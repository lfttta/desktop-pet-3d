"""Build and verify Godette only. Optional --rebuild-assets uses Blender."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

from verify_stage2_package import verify_package
from verify_recovery_visuals import verify as verify_visuals
from verify_godette_desktop import verify as verify_desktop

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / 'app'
REPORT = ROOT / 'build/stage2/reports'
OUTPUT = ROOT / 'dist/stage-2-godette'
ENGINE = Path(r'D:\danjigame\GODDOT  WORD\Godot_v4.7.2-stable_win64_console.exe')
BLENDER = Path(r'D:\steam\steamapps\common\Blender\blender.exe')


def run(name: str, command: list[str], marker: str = '', timeout: int = 300) -> str:
    REPORT.mkdir(parents=True, exist_ok=True)
    log = REPORT / (name + '.log')
    with log.open('w', encoding='utf-8') as stream:
        result = subprocess.run(command, cwd=ROOT, stdout=stream, stderr=subprocess.STDOUT,
                                timeout=timeout, creationflags=subprocess.CREATE_NO_WINDOW)
    text = log.read_text('utf-8', errors='replace')
    if result.returncode or (marker and marker not in text) or 'SCRIPT ERROR:' in text or '\nERROR:' in text:
        raise RuntimeError(f'{name} failed ({result.returncode}):\n{text[-6000:]}')
    print('PASS ' + name, flush=True)
    return text


def godot(name: str, script: str, marker: str, render: bool = False, arguments: list[str] | None = None) -> None:
    command = [str(ENGINE), '--path', str(APP)]
    if not render:
        command.append('--headless')
    run(name, command + ['--script', script] + (['--'] + arguments if arguments else []), marker)


def manifest(package: Path) -> None:
    files = [{'path': p.relative_to(package).as_posix(), 'size': p.stat().st_size,
              'sha256': hashlib.sha256(p.read_bytes()).hexdigest()}
             for p in sorted(package.rglob('*')) if p.is_file() and p.name != 'manifest.json']
    (package / 'manifest.json').write_text(json.dumps(files, indent=2), 'utf-8')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rebuild-assets', action='store_true')
    args = parser.parse_args()
    if sys.prefix == sys.base_prefix or sys.version_info[:3] != (3, 13, 14):
        raise SystemExit('Use .venv/Scripts/python.exe (Python 3.13.14)')
    REPORT.mkdir(parents=True, exist_ok=True)
    run('prepare', [sys.executable, '-B', 'tools/assets/prepare_stage2.py'])
    if args.rebuild_assets:
        run('convert', [str(BLENDER), '--background', '--factory-startup', '--python-exit-code', '1', '--python', 'tools/assets/convert_stage2.py'])
    run('native-build', [str(ROOT / 'native/build.cmd'), '--stage1'])
    for name, marker in [('window_bridge_test', 'hidden-HWND native assertions'), ('lifetime_test', 'lifetime/hotkey assertions, 0 failures'), ('stage1_test', 'stage1 subprocess/resource assertions')]:
        run(name, [str(ROOT / f'native/build/{name}.exe')], marker)
    run('import', [str(ENGINE), '--headless', '--path', str(APP), '--editor', '--import', '--quit'])
    if args.rebuild_assets:
        godot('bake', str(ROOT / 'tools/assets/bake_stage2.gd'), 'STAGE2_BAKED')
        godot('staged-baseline', str(ROOT / 'tools/assets/verify_recovery.gd'), 'GODETTE_REGRESSION failures=0', arguments=['--staged'])
        staged = ROOT / 'build/stage2/godette.tres'
        # os.replace publishes only the tested cache; baseline stays untouched.
        destination = APP / 'resources/animations/godette.tres'
        temporary = destination.with_suffix('.tres.new')
        shutil.copy2(staged, temporary)
        os.replace(temporary, destination)
    run('python-tests', [sys.executable, '-B', '-m', 'pytest', 'tests/service', 'tests/assets', '-q', '-p', 'no:cacheprovider', '--basetemp', str(ROOT / 'build/stage2/test-python')])
    godot('baseline', str(ROOT / 'tools/assets/verify_recovery.gd'), 'GODETTE_REGRESSION failures=0')
    godot('runtime', str(ROOT / 'tools/assets/verify_recovery_runtime.gd'), 'failures=0')
    for name, marker in [('protocol', 'PROTOCOL_TESTS'), ('app_settings', 'APP_SETTINGS_TESTS'), ('windows', 'WINDOW_TESTS'), ('service_client', 'SERVICE_CLIENT_TESTS'), ('chat_framework', 'CHAT_FRAMEWORK_TESTS'), ('stage2_libraries', 'STAGE2_LIBRARY_TESTS'), ('stage2_controllers', 'STAGE2_CONTROLLER_TESTS'), ('stage2_character_catalog', 'STAGE2_CATALOG_TESTS'), ('stage2_character_view', 'STAGE2_CHARACTER_VIEW_TESTS'), ('godette_performance', 'GODETTE_PERFORMANCE'), ('godette_lifecycle', 'GODETTE_LIFECYCLE'), ('desktop_settings', 'DESKTOP_SETTINGS_TESTS'), ('emotion_presenter', 'EMOTION_PRESENTER_TESTS')]:
        godot(name, f'res://tests/test_{name}.gd', marker)
        if 'failures=0' not in (REPORT / (name + '.log')).read_text('utf-8'):
            raise RuntimeError(name + ' did not confirm zero failures')
    godot('preview', 'res://tests/test_godette_preview.gd', 'GODETTE_PREVIEW failures=0', render=True)
    godot('review-regressions', 'res://tests/test_godette_review_regressions.gd', 'GODETTE_REVIEW_REGRESSIONS failures=0')
    godot('poses', str(ROOT / 'tools/assets/render_recovery.gd'), 'GODETTE_RENDER frames=70 framing_failures=0', render=True)
    candidate = ROOT / ('build/stage2/package-' + time.strftime('%Y%m%d-%H%M%S'))
    candidate.mkdir(parents=True, exist_ok=False)
    run('service-build', [sys.executable, '-B', 'tools/build_service.py', '--output', str(candidate / 'service')])
    run('frozen-service-tests', [sys.executable, '-B', '-m', 'pytest', 'tests/service', '-q', '-p', 'no:cacheprovider', '--service-exe', str(candidate / 'service/pet-service.exe'), '--basetemp', str(ROOT / 'build/stage2/test-frozen')])
    run('export', [str(ENGINE), '--headless', '--path', str(APP), '--export-release', 'Windows', str(candidate / 'Pet3D.exe')])
    for filename in ['README-stage2.md', 'THIRD_PARTY_NOTICES.md']:
        shutil.copy2(ROOT / filename, candidate / filename)
    shutil.copy2(ROOT / 'tools/Preview.cmd', candidate / 'Preview.cmd')
    licenses = candidate / 'licenses'
    licenses.mkdir()
    for filename, source in {'vrm-LICENSE': APP / 'addons/vrm/LICENSE', 'mtoon-LICENSE': APP / 'addons/mtoon/LICENSE', 'godot-LICENSE.txt': ROOT / 'native/vendor/godot/LICENSE.txt', 'assets-ATTRIBUTION.txt': APP / 'models/ATTRIBUTION.txt'}.items():
        if source.is_file():
            shutil.copy2(source, licenses / filename)
    manifest(candidate)
    verify_package(candidate, REPORT)
    verify_visuals(candidate, REPORT / 'packaged-visuals')
    verify_desktop(candidate, REPORT)
    if OUTPUT.exists():
        archive = ROOT / ('build/stage2/previous-package-' + time.strftime('%Y%m%d-%H%M%S'))
        if OUTPUT.resolve().parent != (ROOT / 'dist').resolve():
            raise RuntimeError('Output redirected outside dist')
        shutil.move(str(OUTPUT), str(archive))
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(candidate), str(OUTPUT))
    report = json.loads((REPORT / 'package-verification.json').read_text('utf-8'))
    report['package'] = str(OUTPUT)
    (REPORT / 'package-verification.json').write_text(json.dumps(report, indent=2), 'utf-8')
    print('GODETTE_STAGE2_BUILD_OK ' + str(OUTPUT), flush=True)


if __name__ == '__main__':
    main()
