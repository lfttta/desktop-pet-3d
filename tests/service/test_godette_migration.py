import json
from copy import deepcopy

import pytest

from service.settings import DEFAULTS, SettingsStore, ValidationError


@pytest.mark.parametrize('old_id', ['swordsman', 'alicia', 'soldier', 'cartoon_skeleton', 'man'])
def test_old_character_migrates_without_losing_other_settings(tmp_path, old_id):
    previous = deepcopy(DEFAULTS)
    previous.update(character_id=old_id, user_name='Keep me', scale=1.75, screen_id='screen-2',
                    placement={'x': 0.3, 'y': 0.8, 'edge_x': 0, 'edge_y': 1})
    path = tmp_path / 'settings.json'
    path.write_text(json.dumps(previous), 'utf-8')
    expected = {**previous, 'character_id': 'godette'}
    store = SettingsStore(tmp_path)
    assert store.settings == expected
    assert json.loads(path.read_text('utf-8')) == expected
    assert not list(tmp_path.glob('*.broken-*'))
    assert SettingsStore(tmp_path).settings == expected
    with pytest.raises(ValidationError):
        store.update({'character_id': old_id})


def test_migration_write_failure_preserves_original(tmp_path, monkeypatch):
    import service.settings as settings
    previous = {**deepcopy(DEFAULTS), 'character_id': 'alicia', 'user_name': 'Keep me'}
    path = tmp_path / 'settings.json'
    path.write_text(json.dumps(previous), 'utf-8')
    before = path.read_bytes()
    def fail(*_args):
        raise PermissionError('locked')
    monkeypatch.setattr(settings, 'atomic_write', fail)
    with pytest.raises(OSError):
        SettingsStore(tmp_path)
    assert path.read_bytes() == before
    assert not list(tmp_path.glob('*.broken-*'))
