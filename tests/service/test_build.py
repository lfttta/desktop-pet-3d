import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_build_console_onedir_and_launch_from_chinese_path(request):
    if not request.config.getoption('--build-service'):
        pytest.skip('Pass --build-service to run real PyInstaller build')
    result = subprocess.run([sys.executable, '-B', str(ROOT / 'tools/build_service.py')],
                            cwd=ROOT, capture_output=True, timeout=180)
    assert result.returncode == 0, result.stderr.decode('utf-8', errors='replace')
    package = ROOT / 'dist/stage-1/service'
    exe = package / 'pet-service.exe'
    assert exe.is_file()
    assert list(package.rglob('python313.dll'))
    assert list(package.rglob('_sqlite3.pyd'))
    raw = exe.read_bytes()
    pe = struct.unpack_from('<I', raw, 0x3C)[0]
    assert raw[pe:pe + 4] == b'PE\0\0'
    assert struct.unpack_from('<H', raw, pe + 24 + 68)[0] == 3, 'Must preserve console stdio subsystem'
    chinese = ROOT / 'tests/service/打包验证 中文 空格/service'
    shutil.copytree(package, chinese, dirs_exist_ok=True)
    env = os.environ.copy()
    env['PATH'] = str(Path(os.environ.get('SystemRoot', 'C:/Windows')) / 'System32')
    env.pop('PYTHONHOME', None)
    env.pop('PYTHONPATH', None)
    requests = [
        {'version': 1, 'id': '中文', 'type': 'hello', 'payload': {'protocol_version': 1}},
        {'version': 1, 'id': '停止', 'type': 'shutdown', 'payload': {}},
    ]
    result = subprocess.run([str(chinese / 'pet-service.exe'), '--data-dir', str(chinese.parent / '数据')],
                            input=''.join(json.dumps(item, ensure_ascii=False) + '\n' for item in requests).encode('utf-8'),
                            capture_output=True, cwd=chinese.parent, env=env, timeout=20)
    assert result.returncode == 0, result.stderr
    replies = [json.loads(line) for line in result.stdout.decode('utf-8').splitlines()]
    assert [(reply['id'], reply['type']) for reply in replies] == [('中文', 'ready'), ('停止', 'result')]
