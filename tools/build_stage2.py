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
# The delivered package ships exactly these characters; everything else is an
# asset-line contract that must still bake and verify.
PACKAGE_CHARACTER_IDS = ['godette']


def humanoid_contracts() -> dict[str, dict]:
    """Every schema v2 contract bound to humanoid_core, keyed by character id."""
    contracts = {}
    for path in sorted((ROOT / 'characters').glob('*.json')):
        if path.name.endswith('.local.json'):
            continue
        config = json.loads(path.read_text('utf-8'))
        if not isinstance(config, dict) or config.get('schema_version', 0) < 2:
            continue
        if not str(config.get('motion_library', '')).startswith('humanoid_core'):
            continue
        contracts[str(config['id'])] = config
    return contracts


def manifest_actions() -> int:
    manifest = json.loads((ROOT / 'animations/humanoid_core/manifest.json').read_text('utf-8'))
    return len(manifest['actions'])


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
    # Failures of contracts outside PACKAGE_CHARACTER_IDS are recorded and
    # reported, but never stop the packaged character's library from being
    # produced, tested, and published.
    non_package_failures: list[str] = []
    if args.rebuild_assets:
        # Never let a stale staged library stand in for a character whose bake
        # just failed.
        for stale in (ROOT / 'build/stage2').glob('*.tres'):
            stale.unlink()
        try:
            godot('bake', str(ROOT / 'tools/assets/bake_stage2.gd'), 'STAGE2_BAKED')
        except RuntimeError as error:
            non_package_failures.append('bake: ' + str(error).splitlines()[0])
            print('WARN bake reported contract failures; checking the packaged library', flush=True)
        if not (ROOT / 'build/stage2/godette.tres').is_file():
            raise RuntimeError('bake did not produce build/stage2/godette.tres')
        godot('staged-baseline', str(ROOT / 'tools/assets/verify_recovery.gd'), 'GODETTE_REGRESSION failures=0', arguments=['--staged'])
        # Every baked contract, not only the packaged one, must be reachable from
        # its runtime animation_library. Non-package libraries stay out of the
        # delivery through export_presets.cfg's exclude_filter.
        destinations = APP / 'resources/animations'
        for staged in sorted((ROOT / 'build/stage2').glob('*.tres')):
            destination = destinations / staged.name
            # os.replace publishes only the tested cache; baseline stays untouched.
            temporary = destination.with_suffix('.tres.new')
            shutil.copy2(staged, temporary)
            os.replace(temporary, destination)
    run('python-tests', [sys.executable, '-B', '-m', 'pytest', 'tests/service', 'tests/assets', '-q', '-p', 'no:cacheprovider', '--basetemp', str(ROOT / 'build/stage2/test-python')])
    godot('baseline', str(ROOT / 'tools/assets/verify_recovery.gd'), 'GODETTE_REGRESSION failures=0')
    godot('runtime', str(ROOT / 'tools/assets/verify_recovery_runtime.gd'), 'failures=0')
    for name, marker in [('protocol', 'PROTOCOL_TESTS'), ('app_settings', 'APP_SETTINGS_TESTS'), ('windows', 'WINDOW_TESTS'), ('service_client', 'SERVICE_CLIENT_TESTS'), ('chat_framework', 'CHAT_FRAMEWORK_TESTS'), ('action_menu', 'ACTION_MENU_TESTS'), ('action_wiring', 'ACTION_WIRING_TESTS'), ('v3_core', 'V3_CORE_TESTS'), ('v3_panels', 'V3_PANELS_TESTS'), ('stage2_libraries', 'STAGE2_LIBRARY_TESTS'), ('stage2_controllers', 'STAGE2_CONTROLLER_TESTS'), ('stage2_character_catalog', 'STAGE2_CATALOG_TESTS'), ('stage2_character_view', 'STAGE2_CHARACTER_VIEW_TESTS'), ('godette_performance', 'GODETTE_PERFORMANCE'), ('godette_lifecycle', 'GODETTE_LIFECYCLE'), ('desktop_settings', 'DESKTOP_SETTINGS_TESTS'), ('emotion_presenter', 'EMOTION_PRESENTER_TESTS'), ('motion_physics', 'MOTION_PHYSICS_TESTS'), ('motion_wiring', 'MOTION_WIRING_TESTS'), ('walk_plan', 'WALK_PLAN_TESTS'), ('behavior_controller', 'BEHAVIOR_CONTROLLER_TESTS'), ('walk_wiring', 'WALK_WIRING_TESTS'), ('leg_contact', 'LEG_CONTACT_TESTS'), ('speech_bubble', 'SPEECH_BUBBLE_TESTS'), ('memory_panel', 'MEMORY_PANEL_TESTS')]:
        godot(name, f'res://tests/test_{name}.gd', marker)
        if 'failures=0' not in (REPORT / (name + '.log')).read_text('utf-8'):
            raise RuntimeError(name + ' did not confirm zero failures')
    godot('preview', 'res://tests/test_godette_preview.gd', 'GODETTE_PREVIEW failures=0', render=True)
    godot('review-regressions', 'res://tests/test_godette_review_regressions.gd', 'GODETTE_REVIEW_REGRESSIONS failures=0')
    # Frame counts follow the manifest and the packaged contracts, not a constant.
    package_contracts = [cid for cid in humanoid_contracts() if cid in PACKAGE_CHARACTER_IDS]
    frames = len(package_contracts) * manifest_actions() * 10
    godot('poses', str(ROOT / 'tools/assets/render_recovery.gd'), f'GODETTE_RENDER frames={frames} framing_failures=0', render=True)
    # Non-package characters never ship, but their staged bakes must pass the
    # same asset-line gate: every baked contract outside the package catalog.
    contracts = humanoid_contracts()
    for staged in sorted((ROOT / 'build/stage2').glob('*.tres')):
        character_id = staged.stem
        if character_id in PACKAGE_CHARACTER_IDS:
            continue
        if character_id not in contracts:
            raise RuntimeError(f'{staged.name} has no humanoid_core contract')
        try:
            godot('character-assets-' + character_id, str(ROOT / 'tools/assets/verify_character_assets.gd'),
                  f'CHARACTER_ASSETS character={character_id} frames={manifest_actions() * 10} failures=0',
                  render=True, arguments=[f'--character={character_id}'])
        except RuntimeError as error:
            non_package_failures.append(character_id + ': ' + str(error).splitlines()[0])
            print(f'WARN non-package contract {character_id} failed its asset gate', flush=True)
    if non_package_failures:
        REPORT.mkdir(parents=True, exist_ok=True)
        (REPORT / 'non-package-failures.json').write_text(json.dumps(non_package_failures, indent=2), 'utf-8')
        print('WARN non-package contracts reported failures: ' + '; '.join(non_package_failures), flush=True)
    candidate = ROOT / ('build/stage2/package-' + time.strftime('%Y%m%d-%H%M%S'))
    candidate.mkdir(parents=True, exist_ok=False)
    run('service-build', [sys.executable, '-B', 'tools/build_service.py', '--output', str(candidate / 'service')])
    run('frozen-service-tests', [sys.executable, '-B', '-m', 'pytest', 'tests/service', '-q', '-p', 'no:cacheprovider', '--service-exe', str(candidate / 'service/pet-service.exe'), '--basetemp', str(ROOT / 'build/stage2/test-frozen')])
    run('export', [str(ENGINE), '--headless', '--path', str(APP), '--export-release', 'Windows', str(candidate / 'Pet3D.exe')])
    for filename in ['README-stage2.md', 'THIRD_PARTY_NOTICES.md']:
        shutil.copy2(ROOT / filename, candidate / filename)
    # The app reads data/expression/phrases.json at runtime (the line it says
    # when clicked). The service carries its own copy under service/_internal/data,
    # but the app looks next to the executable, so the package needs this too.
    shutil.copytree(ROOT / 'data', candidate / 'data')
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
