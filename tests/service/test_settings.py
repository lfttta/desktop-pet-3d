import json
from pathlib import Path

import pytest


def test_defaults_persist_and_returned_values_are_detached(tmp_path):
    from service.settings import SettingsStore
    store = SettingsStore(tmp_path)
    assert store.settings == {
        'schema_version': 2, 'character_id': 'godette', 'persona_id': 'godette',
        'scale': 1.0, 'screen_id': '', 'user_name': '你',
        'placement': {'x': 1.0, 'y': 1.0, 'edge_x': 1, 'edge_y': 1},
    }
    snapshot = store.settings
    snapshot['placement']['x'] = 0
    assert store.settings['placement']['x'] == 1.0
    saved = store.update({'character_id': 'godette', 'scale': 0.5,
                          'user_name': '小明', 'screen_id': '显示器 一',
                          'placement': {'x': 0, 'y': 0.5, 'edge_x': -1, 'edge_y': 0}})
    assert json.loads((tmp_path / 'settings.json').read_text('utf-8')) == saved
    assert SettingsStore(tmp_path).settings == saved
    assert type(saved['placement']['x']) is float


@pytest.mark.parametrize('patch', [
    {'scale': True}, {'scale': '1.0'}, {'scale': 0.49}, {'scale': 2.01},
    {'scale': float('nan')}, {'scale': float('inf')}, {'character_id': 'other'}, {'character_id': 'swordsman'},
    {'user_name': '你' * 81}, {'user_name': None}, {'screen_id': 1},
    {'schema_version': 2}, {'api_key': 'no'}, {'placement': {'x': 0}},
    {'placement': {'x': True, 'y': 0, 'edge_x': 0, 'edge_y': 0}},
    {'placement': {'x': 0, 'y': 1.1, 'edge_x': 0, 'edge_y': 0}},
    {'placement': {'x': 0, 'y': 0, 'edge_x': 0.5, 'edge_y': 0}},
    {'placement': {'x': 0, 'y': 0, 'edge_x': True, 'edge_y': 0}},
    {'placement': {'x': 0, 'y': 0, 'edge_x': 2, 'edge_y': 0}},
    {'user_name': '\ud800'}, [], None,
])
def test_invalid_patch_never_changes_disk_or_memory(tmp_path, patch):
    from service.settings import SettingsStore, ValidationError
    store = SettingsStore(tmp_path)
    before = store.settings
    raw = (tmp_path / 'settings.json').read_bytes()
    with pytest.raises(ValidationError):
        store.update(patch)
    assert store.settings == before
    assert (tmp_path / 'settings.json').read_bytes() == raw


def test_replace_failure_preserves_last_success_and_cleans_temp(tmp_path, monkeypatch):
    import service.settings as settings
    store = settings.SettingsStore(tmp_path)
    store.update({'user_name': '之前'})
    original = (tmp_path / 'settings.json').read_bytes()
    attempts = []

    def deny(source, destination):
        attempts.append(source)
        raise PermissionError('locked')

    with monkeypatch.context() as scoped:
        scoped.setattr(settings.os, 'replace', deny)
        with pytest.raises(OSError):
            store.update({'user_name': '不应保存'})
    assert 1 < len(attempts) <= 5
    assert store.settings['user_name'] == '之前'
    assert (tmp_path / 'settings.json').read_bytes() == original
    assert not list(tmp_path.glob('*.tmp'))
    assert store.update({'user_name': '恢复'})['user_name'] == '恢复'


def test_stage2_character_catalog_is_the_validation_source(tmp_path):
    from service.settings import SettingsStore, ALLOWED_CHARACTER_IDS
    assert ALLOWED_CHARACTER_IDS == frozenset({'godette'})
    store = SettingsStore(tmp_path)
    assert store.update({'character_id': 'godette'})['character_id'] == 'godette'


def test_file_is_fsynced_before_replace_and_transient_lock_recovers(tmp_path, monkeypatch):
    import service.settings as settings
    store = settings.SettingsStore(tmp_path)
    real_fsync, real_replace = settings.os.fsync, settings.os.replace
    events = []

    def fsync(fd):
        events.append('fsync')
        return real_fsync(fd)

    def replace(src, dst):
        events.append('replace')
        if events.count('replace') == 1:
            raise PermissionError('transient')
        return real_replace(src, dst)

    monkeypatch.setattr(settings.os, 'fsync', fsync)
    monkeypatch.setattr(settings.os, 'replace', replace)
    assert store.update({'scale': 2})['scale'] == 2.0
    assert events == ['fsync', 'replace', 'replace']
    assert type(store.settings['scale']) is float


def test_fsync_failure_is_not_a_success(tmp_path, monkeypatch):
    import service.settings as settings
    store = settings.SettingsStore(tmp_path)
    before = (tmp_path / 'settings.json').read_bytes()

    def fail(fd):
        raise OSError('disk full')

    monkeypatch.setattr(settings.os, 'fsync', fail)
    with pytest.raises(OSError):
        store.update({'scale': 1.5})
    assert store.settings['scale'] == 1.0
    assert (tmp_path / 'settings.json').read_bytes() == before
    assert not list(tmp_path.glob('*.tmp'))


@pytest.mark.parametrize('raw', [b'\xffbroken', b'{bad json', b'{"schema_version":2}',
                                 b'{"schema_version":true}', b'{"schema_version":1}',
                                 b'{"schema_version":1,"schema_version":2}'])
def test_bad_config_backed_up_exactly_and_recovered_once(tmp_path, raw):
    from service.settings import SettingsStore
    config = tmp_path / 'settings.json'
    config.write_bytes(raw)
    first = SettingsStore(tmp_path)
    assert first.warnings
    assert first.settings['character_id'] == 'godette'
    backups = list(tmp_path.glob('settings.json.broken-*'))
    assert len(backups) == 1 and backups[0].read_bytes() == raw
    assert SettingsStore(tmp_path).warnings == []
    config.write_bytes(raw)
    assert SettingsStore(tmp_path).warnings
    backups = list(tmp_path.glob('settings.json.broken-*'))
    assert len(backups) == 2 and all(p.read_bytes() == raw for p in backups)


def test_backup_failure_does_not_destroy_broken_original(tmp_path, monkeypatch):
    from service.settings import SettingsStore
    config = tmp_path / 'settings.json'
    config.write_bytes(b'broken original')
    original_open = Path.open

    def deny_backup(path, *args, **kwargs):
        if '.broken-' in path.name:
            raise PermissionError('backup denied')
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'open', deny_backup)
    with pytest.raises(OSError):
        SettingsStore(tmp_path)
    assert config.read_bytes() == b'broken original'
