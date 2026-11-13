"""Schema v8 hotkey settings: text validation and the v7 migration."""

from copy import deepcopy
import json

import pytest

from service.settings import (
    DEFAULTS,
    HOTKEYS_DEFAULTS,
    SCHEMA_VERSION,
    SettingsStore,
    ValidationError,
    validate_document,
)


def _write(tmp_path, document):
    path = tmp_path / 'settings.json'
    path.write_bytes((json.dumps(document, ensure_ascii=False) + '\n').encode('utf-8'))
    return path


def test_v8_defaults_add_hotkeys_group():
    assert SCHEMA_VERSION == 8
    assert DEFAULTS['hotkeys'] == {
        'recover': 'Ctrl+Alt+F10', 'exit': 'Ctrl+Alt+Q',
    }
    assert DEFAULTS['hotkeys'] == HOTKEYS_DEFAULTS
    assert validate_document(deepcopy(DEFAULTS)) == DEFAULTS


def test_v7_document_gains_hotkeys_and_keeps_values(tmp_path):
    v7 = deepcopy(DEFAULTS)
    v7['schema_version'] = 7
    del v7['hotkeys']
    v7['scale'] = 0.9
    v7['user_name'] = '小陈'
    v7['bubble']['duration_seconds'] = 9
    v7['greeting']['on_start'] = False
    path = _write(tmp_path, v7)

    store = SettingsStore(tmp_path)
    assert store.settings['schema_version'] == 8
    assert store.settings['hotkeys'] == HOTKEYS_DEFAULTS
    assert store.settings['scale'] == 0.9
    assert store.settings['user_name'] == '小陈'
    assert store.settings['bubble']['duration_seconds'] == 9
    assert store.settings['greeting']['on_start'] is False
    assert any('schema version 8' in warning for warning in store.warnings)
    assert json.loads(path.read_text('utf-8')) == store.settings
    assert SettingsStore(tmp_path).warnings == []


@pytest.mark.parametrize('value', [
    'Ctrl+Alt+F10',
    'ctrl+alt+f10',
    'Shift+Ctrl+Q',
    'Ctrl+Alt+Shift+F24',
    'Win+Space',
    'Ctrl+Alt+PageUp',
    'Ctrl+Alt+9',
    'Alt+PrintScreen',
])
def test_hotkey_validation_accepts_supported_bindings(tmp_path, value):
    store = SettingsStore(tmp_path)
    saved = store.update({'hotkeys': {'recover': value}})
    assert saved['hotkeys']['recover'] == value
    assert saved['hotkeys']['exit'] == HOTKEYS_DEFAULTS['exit']


@pytest.mark.parametrize('value', [
    '',
    'F10',
    'Ctrl',
    'Ctrl+Alt',
    'Ctrl+Alt+',
    '+Q',
    'Ctrl+Alt+Q+F1',
    'Ctrl+Ctrl+Q',
    'Ctrl+Control+Q',
    'Ctrl+Alt+F25',
    'Ctrl+Alt+F0',
    'Ctrl+Alt+中',
    'Ctrl+Alt+Unknown',
    7,
    True,
])
def test_hotkey_validation_rejects_bad_bindings(tmp_path, value):
    store = SettingsStore(tmp_path)
    before = store.settings
    raw = (tmp_path / 'settings.json').read_bytes()
    with pytest.raises(ValidationError):
        store.update({'hotkeys': {'recover': value}})
    assert store.settings == before
    assert (tmp_path / 'settings.json').read_bytes() == raw


def test_identical_hotkeys_are_rejected(tmp_path):
    store = SettingsStore(tmp_path)
    with pytest.raises(ValidationError):
        store.update({'hotkeys': {'recover': 'Ctrl+Alt+Q'}})
    document = deepcopy(DEFAULTS)
    document['hotkeys'] = {'recover': 'Alt+F9', 'exit': 'alt+f9'}
    with pytest.raises(ValidationError):
        validate_document(document)


def test_hotkey_patch_keeps_the_other_binding(tmp_path):
    store = SettingsStore(tmp_path)
    saved = store.update({'hotkeys': {'exit': 'Ctrl+Shift+Esc'}})
    assert saved['hotkeys'] == {
        'recover': HOTKEYS_DEFAULTS['recover'], 'exit': 'Ctrl+Shift+Esc',
    }
    assert SettingsStore(tmp_path).settings['hotkeys'] == saved['hotkeys']
