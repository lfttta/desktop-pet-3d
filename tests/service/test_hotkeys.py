"""Schema v10 hotkey settings: text validation and the v7/v8/v9 migrations."""

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


def test_v10_defaults_keep_hotkeys_group():
    assert SCHEMA_VERSION == 11
    assert DEFAULTS['hotkeys'] == {
        'recover': 'Ctrl+Alt+F10', 'exit': 'Ctrl+Alt+Q', 'chat': 'Ctrl+Alt+C',
    }
    assert DEFAULTS['hotkeys'] == HOTKEYS_DEFAULTS
    assert validate_document(deepcopy(DEFAULTS)) == DEFAULTS


def test_v7_document_gains_hotkeys_and_keeps_values(tmp_path):
    v7 = deepcopy(DEFAULTS)
    v7['schema_version'] = 7
    del v7['hotkeys']
    # The v9 tone did not exist in v7; the migration must backfill it.
    del v7['expression']['tone']
    # The v11 vision group did not exist in v7 either.
    del v7['vision']
    v7['scale'] = 0.9
    v7['user_name'] = '小陈'
    v7['bubble']['duration_seconds'] = 9
    v7['greeting']['on_start'] = False
    path = _write(tmp_path, v7)

    store = SettingsStore(tmp_path)
    assert store.settings['schema_version'] == SCHEMA_VERSION
    assert store.settings['hotkeys'] == HOTKEYS_DEFAULTS
    assert store.settings['expression']['tone'] == 'godette'
    assert store.settings['scale'] == 0.9
    assert store.settings['user_name'] == '小陈'
    assert store.settings['bubble']['duration_seconds'] == 9
    assert store.settings['greeting']['on_start'] is False
    assert any(f'schema version {SCHEMA_VERSION}' in warning for warning in store.warnings)
    assert json.loads(path.read_text('utf-8')) == store.settings
    assert SettingsStore(tmp_path).warnings == []


def test_v9_document_gains_chat_and_keeps_bindings(tmp_path):
    v9 = deepcopy(DEFAULTS)
    v9['schema_version'] = 9
    del v9['hotkeys']['chat']
    # The v11 vision group did not exist in v9 either.
    del v9['vision']
    v9['hotkeys']['recover'] = 'Alt+F9'
    v9['hotkeys']['exit'] = 'Alt+F8'
    path = _write(tmp_path, v9)

    store = SettingsStore(tmp_path)
    assert store.settings['schema_version'] == SCHEMA_VERSION
    assert store.settings['hotkeys'] == {
        'recover': 'Alt+F9', 'exit': 'Alt+F8', 'chat': 'Ctrl+Alt+C',
    }
    assert any(f'schema version {SCHEMA_VERSION}' in warning for warning in store.warnings)
    assert json.loads(path.read_text('utf-8')) == store.settings
    assert SettingsStore(tmp_path).warnings == []


@pytest.mark.parametrize('version', [8, 9])
@pytest.mark.parametrize('field', ['recover', 'exit'])
def test_old_document_whose_binding_owns_the_chat_default_is_kept(tmp_path, version, field):
    """A legal pre-v10 file must not be reset because v10 adds a chat default.

    v8/v9 files may already store Ctrl+Alt+C for recover or exit. Blindly
    defaulting chat to the same combination made validation fail, and the store
    answered by backing the whole document up and restoring the defaults.
    """
    document = deepcopy(DEFAULTS)
    document['schema_version'] = version
    # The v11 vision group did not exist in v8/v9.
    del document['vision']
    document['hotkeys'] = {
        'recover': 'Ctrl+Alt+F10', 'exit': 'Ctrl+Alt+Q',
    }
    if version == 9:
        # v9's shape is the v10 shape minus hotkeys.chat.
        pass
    document['hotkeys'][field] = 'Ctrl+Alt+C'
    document['scale'] = 1.37
    document['user_name'] = '小林'
    path = _write(tmp_path, document)

    store = SettingsStore(tmp_path)
    assert store.settings['schema_version'] == SCHEMA_VERSION
    assert store.settings['scale'] == 1.37
    assert store.settings['user_name'] == '小林'
    assert store.settings['hotkeys'][field] == 'Ctrl+Alt+C'
    assert store.settings['hotkeys']['chat'] != 'Ctrl+Alt+C'
    assert not any('defaults restored' in warning for warning in store.warnings)
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
    'ctrl + alt + f9',
])
def test_hotkey_validation_accepts_supported_bindings(tmp_path, value):
    store = SettingsStore(tmp_path)
    saved = store.update({'hotkeys': {'recover': value}})
    assert saved['hotkeys']['recover'] == value
    assert saved['hotkeys']['exit'] == HOTKEYS_DEFAULTS['exit']
    assert saved['hotkeys']['chat'] == HOTKEYS_DEFAULTS['chat']


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


def test_hotkey_validation_applies_to_the_chat_field(tmp_path):
    store = SettingsStore(tmp_path)
    saved = store.update({'hotkeys': {'chat': 'alt + shift + c'}})
    assert saved['hotkeys']['chat'] == 'alt + shift + c'
    assert saved['hotkeys']['recover'] == HOTKEYS_DEFAULTS['recover']
    before = store.settings
    with pytest.raises(ValidationError):
        store.update({'hotkeys': {'chat': 'F9'}})
    assert store.settings == before


def test_identical_hotkeys_are_rejected(tmp_path):
    store = SettingsStore(tmp_path)
    with pytest.raises(ValidationError):
        store.update({'hotkeys': {'recover': 'Ctrl+Alt+Q'}})
    with pytest.raises(ValidationError):
        store.update({'hotkeys': {'chat': 'ctrl+alt+q'}})
    with pytest.raises(ValidationError):
        store.update({'hotkeys': {'chat': 'Ctrl+Alt+F10'}})
    document = deepcopy(DEFAULTS)
    document['hotkeys'] = {'recover': 'Alt+F9', 'exit': 'alt+f9', 'chat': 'Alt+F8'}
    with pytest.raises(ValidationError):
        validate_document(document)
    document['hotkeys'] = {'recover': 'Alt+F9', 'exit': 'Alt+F8', 'chat': 'alt+f8'}
    with pytest.raises(ValidationError):
        validate_document(document)


@pytest.mark.parametrize('recover', [
    'ctrl + alt + c',
    'Alt+Ctrl+C',
    'control+alt+c',
])
def test_synonymous_hotkeys_are_rejected(tmp_path, recover):
    """Spacing, order and modifier aliases do not make a binding distinct."""
    store = SettingsStore(tmp_path)
    before = store.settings
    with pytest.raises(ValidationError):
        store.update({'hotkeys': {'recover': recover}})
    assert store.settings == before
    document = deepcopy(DEFAULTS)
    document['hotkeys']['recover'] = recover
    with pytest.raises(ValidationError):
        validate_document(document)


def test_key_aliases_compare_as_the_same_binding(tmp_path):
    """`escape`/`esc`, `pgdn`/`PageDown` register the same virtual key."""
    document = deepcopy(DEFAULTS)
    document['hotkeys'] = {'recover': 'Ctrl+escape', 'exit': 'ctrl+esc',
                           'chat': 'Alt+Shift+C'}
    with pytest.raises(ValidationError):
        validate_document(document)
    document['hotkeys'] = {'recover': 'Ctrl+pgdn', 'exit': 'Ctrl+PageDown',
                           'chat': 'Alt+Shift+C'}
    with pytest.raises(ValidationError):
        validate_document(document)
    document['hotkeys'] = {'recover': 'Ctrl+Escape', 'exit': 'Ctrl+PageDown',
                           'chat': 'Alt+Shift+C'}
    assert validate_document(document)['hotkeys']['recover'] == 'Ctrl+Escape'


def test_hotkey_patch_keeps_the_other_bindings(tmp_path):
    store = SettingsStore(tmp_path)
    saved = store.update({'hotkeys': {'exit': 'Ctrl+Shift+Esc'}})
    assert saved['hotkeys'] == {
        'recover': HOTKEYS_DEFAULTS['recover'], 'exit': 'Ctrl+Shift+Esc',
        'chat': HOTKEYS_DEFAULTS['chat'],
    }
    assert SettingsStore(tmp_path).settings['hotkeys'] == saved['hotkeys']
