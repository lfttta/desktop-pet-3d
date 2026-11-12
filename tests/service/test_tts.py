"""Speech synthesis contracts: cache, validation, failure mapping and clamps.

The Windows backend is always replaced here; these tests never start PowerShell
or open an audio device.
"""

from copy import deepcopy
import json
from pathlib import Path
import subprocess

import pytest

import service.tts as tts
from service import commands
from service.protocol import ProtocolError
from service.settings import (DEFAULTS, DESKTOP_DEFAULTS, VOICE_DEFAULTS,
                              SettingsStore, ValidationError)

WAV_BYTES = b'RIFF\x24\x00\x00\x00WAVEfake'


def _fake_backend(calls, payload=WAV_BYTES):
    def run(text, destination, voice, rate, volume):
        calls.append({'text': text, 'voice': voice, 'rate': rate, 'volume': volume})
        Path(destination).write_bytes(payload)
    return run


def test_second_synthesis_hits_cache_and_skips_the_backend(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(tts, '_run_powershell', _fake_backend(calls))

    first = tts.synthesize('你好', tmp_path, voice='Microsoft Huihui')
    second = tts.synthesize('你好', tmp_path, voice='Microsoft Huihui')

    assert first['cached'] is False
    assert second['cached'] is True
    assert first['voice'] == second['voice'] == 'Microsoft Huihui'
    assert first['path'] == second['path']
    assert len(calls) == 1
    path = Path(first['path'])
    assert path.is_absolute()
    assert path.parent == (tmp_path / 'tts').resolve()
    assert path.read_bytes() == WAV_BYTES


@pytest.mark.parametrize('text', ['', '   ', '\n\t '])
def test_blank_text_is_invalid_request(tmp_path, monkeypatch, text):
    calls = []
    monkeypatch.setattr(tts, '_run_powershell', _fake_backend(calls))

    with pytest.raises(ProtocolError) as caught:
        tts.synthesize(text, tmp_path)

    assert caught.value.code == 'invalid_request'
    assert calls == []


@pytest.mark.parametrize('error', [
    RuntimeError('no speech engine'),
    FileNotFoundError('powershell.exe'),
    subprocess.TimeoutExpired(['powershell.exe'], 20),
    subprocess.CalledProcessError(1, ['powershell.exe']),
])
def test_backend_failure_maps_to_tts_unavailable(tmp_path, monkeypatch, error):
    def explode(_text, _destination, _voice, _rate, _volume):
        raise error

    monkeypatch.setattr(tts, '_run_powershell', explode)
    with pytest.raises(ProtocolError) as caught:
        tts.synthesize('你好', tmp_path)

    assert caught.value.code == 'tts_unavailable'
    assert not list((tmp_path / 'tts').glob('*'))


def test_text_longer_than_500_characters_is_truncated(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(tts, '_run_powershell', _fake_backend(calls))

    tts.synthesize('字' * 650, tmp_path)

    assert len(calls) == 1
    assert len(calls[0]['text']) == 500
    assert calls[0]['text'] == '字' * 500


def test_rate_and_volume_are_clamped_to_the_backend_limits(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(tts, '_run_powershell', _fake_backend(calls))

    tts.synthesize('低', tmp_path, rate=-99, volume=-5)
    tts.synthesize('高', tmp_path, rate=99, volume=250)

    assert calls == [
        {'text': '低', 'voice': '', 'rate': -10, 'volume': 0},
        {'text': '高', 'voice': '', 'rate': 10, 'volume': 100},
    ]


def test_voice_command_uses_the_settings_group_and_is_registered(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(tts, '_run_powershell', _fake_backend(calls))
    store = SettingsStore(tmp_path)
    store.update({'voice': {'enabled': True, 'voice': 'Microsoft Huihui',
                            'rate': 4, 'volume': 55}})

    event, payload = commands.dispatch(
        {'type': 'tts.synthesize', 'id': 'v1', 'payload': {'text': '你好'}}, store)

    assert event == 'result'
    assert payload['voice'] == 'Microsoft Huihui'
    assert payload['cached'] is False
    assert calls == [{'text': '你好', 'voice': 'Microsoft Huihui',
                      'rate': 4, 'volume': 55}]


def test_voice_is_off_by_default(tmp_path):
    # Speaking is paused: synthesis blocks the service thread, so the default
    # path must not run it. Turning the switch on stays possible.
    store = SettingsStore(tmp_path)
    assert store.settings['voice']['enabled'] is False


def test_disabled_voice_refuses_without_touching_the_backend(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(tts, '_run_powershell', _fake_backend(calls))
    store = SettingsStore(tmp_path)
    store.update({'voice': {'enabled': False}})

    with pytest.raises(ProtocolError) as caught:
        commands.dispatch(
            {'type': 'tts.synthesize', 'id': 'v2', 'payload': {'text': '你好'}}, store)

    assert caught.value.code == 'voice_disabled'
    assert calls == []


def test_voice_settings_validate_rate_volume_and_type(tmp_path):
    store = SettingsStore(tmp_path)
    assert store.settings['voice'] == {
        'enabled': False, 'voice': '', 'rate': 0, 'volume': 70}
    for patch in ({'voice': {'rate': 11}}, {'voice': {'rate': -11}},
                  {'voice': {'volume': 101}}, {'voice': {'volume': -1}},
                  {'voice': {'voice': 5}}, {'voice': {'enabled': 1}},
                  {'voice': {'unknown': True}}):
        with pytest.raises(ValidationError):
            store.update(patch)


def _write_settings(tmp_path, document):
    (tmp_path / 'settings.json').write_bytes(
        (json.dumps(document, ensure_ascii=False) + '\n').encode('utf-8'))


def test_v5_document_without_voice_gains_defaults(tmp_path):
    # A schema 5 file written before the voice group existed; neither v6 group
    # nor autonomy.default_quiet_minutes was there yet either.
    document = {key: value for key, value in deepcopy(DEFAULTS).items()
                if key not in ('voice', 'behavior', 'performance')}
    document['schema_version'] = 5
    del document['autonomy']['default_quiet_minutes']
    _write_settings(tmp_path, document)

    store = SettingsStore(tmp_path)

    assert store.settings['voice'] == VOICE_DEFAULTS
    assert json.loads((tmp_path / 'settings.json').read_text('utf-8')) == store.settings
    assert any('schema version 6' in warning for warning in store.warnings)


def test_v4_document_without_voice_gains_desktop_and_voice_defaults(tmp_path):
    document = deepcopy(DEFAULTS)
    document['schema_version'] = 4
    del document['desktop']
    del document['behavior']
    del document['performance']
    del document['voice']
    del document['autonomy']['cloud_enabled']
    del document['autonomy']['default_quiet_minutes']
    _write_settings(tmp_path, document)

    store = SettingsStore(tmp_path)

    assert store.settings['schema_version'] == 6
    assert store.settings['desktop'] == DESKTOP_DEFAULTS
    assert store.settings['voice'] == VOICE_DEFAULTS
