"""Framework contracts: command registry, capabilities, chat stub and migrations."""

import json

import pytest

from service.commands import dispatch
from service.protocol import ProtocolError
from service.settings import DEFAULTS, SettingsStore


def ready_payload(tmp_path):
    store = SettingsStore(tmp_path)
    event, payload = dispatch({'type': 'hello', 'id': 'hello',
                               'payload': {'protocol_version': 1}}, store)
    assert event == 'ready'
    return store, payload


def test_persona_catalog_keeps_prompt_private():
    from service.persona import public_catalog, resolve
    catalog = public_catalog()
    assert catalog == [{'id': 'godette', 'display_name': 'Godette'}]
    assert 'prompt' not in catalog[0]
    assert resolve('godette')['prompt'] == ''
    assert resolve('missing') is None


def test_hello_advertises_chat_capability_and_personas(tmp_path):
    store, payload = ready_payload(tmp_path)
    assert payload['settings']['persona_id'] == 'godette'
    assert payload['features']['chat'] == {
        'available': False, 'reason': 'not_implemented',
        'commands': ['chat.send', 'chat.cancel'],
    }
    assert payload['personas'] == [{'id': 'godette', 'display_name': 'Godette'}]


def test_chat_send_valid_contract_stays_unavailable(tmp_path):
    store = SettingsStore(tmp_path)
    for payload in ({'text': '你好'}, {'text': 'x' * 8000},
                    {'text': '你好', 'conversation_id': 'c-1'}):
        with pytest.raises(ProtocolError) as caught:
            dispatch({'type': 'chat.send', 'id': 'chat', 'payload': payload}, store)
        assert caught.value.code == 'not_available'
        assert caught.value.request_id == 'chat'


@pytest.mark.parametrize('payload', [
    {}, {'text': ''}, {'text': '   '}, {'text': 1}, {'text': None},
    {'text': 'x' * 8001}, {'text': 'ok', 'conversation_id': 5},
    {'text': 'ok', 'conversation_id': 'x' * 129},
])
def test_chat_send_rejects_invalid_payload(tmp_path, payload):
    store = SettingsStore(tmp_path)
    with pytest.raises(ProtocolError) as caught:
        dispatch({'type': 'chat.send', 'id': 'chat', 'payload': payload}, store)
    assert caught.value.code == 'invalid_request'
    assert caught.value.request_id == 'chat'


def test_chat_cancel_remains_a_placeholder(tmp_path):
    store = SettingsStore(tmp_path)
    assert dispatch({'type': 'chat.cancel', 'id': 'cancel', 'payload': {}}, store) == ('cancelled', {})


def test_memory_and_tts_remain_reserved(tmp_path):
    store = SettingsStore(tmp_path)
    for command in ('memory', 'memory.get', 'tts.synthesize'):
        with pytest.raises(ProtocolError) as caught:
            dispatch({'type': command, 'id': 'reserved', 'payload': {}}, store)
        assert caught.value.code == 'not_available'
    assert dispatch({'type': 'tts.cancel', 'id': 'cancel', 'payload': {}}, store) == ('cancelled', {})
    with pytest.raises(ProtocolError) as caught:
        dispatch({'type': 'nonsense', 'id': 'unknown', 'payload': {}}, store)
    assert caught.value.code == 'unknown_command'


def test_v1_settings_are_migrated_without_losing_values(tmp_path):
    v1 = {
        'schema_version': 1, 'character_id': 'godette', 'scale': 0.75,
        'screen_id': '显示器 一', 'user_name': '小明',
        'placement': {'x': 0.2, 'y': 0.8, 'edge_x': -1, 'edge_y': 1},
    }
    (tmp_path / 'settings.json').write_bytes(
        (json.dumps(v1, ensure_ascii=False) + '\n').encode('utf-8'))
    store = SettingsStore(tmp_path)
    assert store.settings == {**v1, 'schema_version': 2, 'persona_id': 'godette'}
    assert any('schema version 2' in warning for warning in store.warnings)
    assert json.loads((tmp_path / 'settings.json').read_text('utf-8')) == store.settings
    assert SettingsStore(tmp_path).warnings == []


def test_v1_legacy_character_and_schema_migrate_together(tmp_path):
    v1 = {**DEFAULTS, 'schema_version': 1, 'character_id': 'swordsman'}
    v1.pop('persona_id')
    (tmp_path / 'settings.json').write_bytes(
        (json.dumps(v1, ensure_ascii=False) + '\n').encode('utf-8'))
    store = SettingsStore(tmp_path)
    assert store.settings['character_id'] == 'godette'
    assert store.settings['persona_id'] == 'godette'
    assert store.settings['schema_version'] == 2
    assert len(store.warnings) == 2


def test_persona_id_is_validated(tmp_path):
    from service.settings import ValidationError
    store = SettingsStore(tmp_path)
    assert store.update({'persona_id': 'godette'})['persona_id'] == 'godette'
    for value in ('other', '', None, 1):
        with pytest.raises(ValidationError):
            store.update({'persona_id': value})
    assert store.settings['persona_id'] == 'godette'


def test_future_schema_is_backed_up_and_reset(tmp_path):
    future = {**DEFAULTS, 'schema_version': 99}
    raw = (json.dumps(future, ensure_ascii=False) + '\n').encode('utf-8')
    (tmp_path / 'settings.json').write_bytes(raw)
    store = SettingsStore(tmp_path)
    assert store.settings == DEFAULTS
    assert store.warnings
    backups = list(tmp_path.glob('settings.json.broken-*'))
    assert len(backups) == 1 and backups[0].read_bytes() == raw
