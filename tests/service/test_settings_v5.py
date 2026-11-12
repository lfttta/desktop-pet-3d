"""Settings schema v5/v6/v7 contracts: desktop group, cloud switch, behavior,
performance, bubble/greeting and migrations."""

from copy import deepcopy
import json

import pytest

from service.settings import (
    AUTONOMY_DEFAULTS,
    BEHAVIOR_DEFAULTS,
    DEFAULTS,
    DESKTOP_DEFAULTS,
    GROWTH_DEFAULTS,
    AI_EMBEDDING_DEFAULTS,
    MEMORY_ARCHIVE_DEFAULTS,
    MEMORY_COMPACTION_DEFAULTS,
    PERFORMANCE_DEFAULTS,
    SCHEMA_VERSION,
    SettingsStore,
    ValidationError,
    validate_document,
)


def _v4_document():
    """A realistic v4 document: no desktop/behavior/performance groups, the
    original six autonomy keys."""
    document = deepcopy(DEFAULTS)
    document['schema_version'] = 4
    del document['desktop']
    del document['behavior']
    del document['performance']
    del document['bubble']
    del document['greeting']
    del document['autonomy']['cloud_enabled']
    del document['autonomy']['default_quiet_minutes']
    document['scale'] = 0.75
    document['screen_id'] = 'monitor-2'
    document['user_name'] = '小明'
    document['ai']['temperature'] = 0.3
    document['ai']['max_tokens'] = 1200
    document['memory']['max_injected'] = 7
    document['expression']['style']['warmth'] = 0.2
    document['autonomy']['enabled'] = True
    document['autonomy']['quiet_start'] = '22:00'
    document['autonomy']['quiet_end'] = '07:00'
    document['autonomy']['max_per_hour'] = 5
    document['growth']['min_sessions'] = 5
    document['privacy']['log_prompts'] = True
    return document


def _write(tmp_path, document):
    path = tmp_path / 'settings.json'
    path.write_bytes((json.dumps(document, ensure_ascii=False) + '\n').encode('utf-8'))
    return path


def test_default_document_and_fresh_store_validate(tmp_path):
    assert SCHEMA_VERSION == 7
    assert validate_document(deepcopy(DEFAULTS)) == DEFAULTS
    store = SettingsStore(tmp_path)
    assert store.settings == DEFAULTS
    assert validate_document(store.settings) == DEFAULTS
    assert store.settings['desktop'] == DESKTOP_DEFAULTS
    assert store.settings['behavior'] == {'enabled': True}
    assert store.settings['behavior'] == BEHAVIOR_DEFAULTS
    assert store.settings['performance'] == {
        'idle_fps': 30, 'drag_fps': 60, 'hidden_fps': 5,
    }
    assert store.settings['performance'] == PERFORMANCE_DEFAULTS
    assert store.settings['autonomy']['cloud_enabled'] is True
    assert store.settings['autonomy']['default_quiet_minutes'] == 30
    assert json.loads((tmp_path / 'settings.json').read_text('utf-8')) == DEFAULTS


def test_v4_document_migrates_and_fills_desktop_and_cloud_enabled(tmp_path):
    v4 = _v4_document()
    path = _write(tmp_path, v4)

    store = SettingsStore(tmp_path)
    assert store.settings['schema_version'] == 7
    assert store.settings['desktop'] == DESKTOP_DEFAULTS
    assert store.settings['behavior'] == BEHAVIOR_DEFAULTS
    assert store.settings['performance'] == PERFORMANCE_DEFAULTS
    assert store.settings['autonomy']['cloud_enabled'] is True
    assert store.settings['autonomy']['default_quiet_minutes'] == 30
    # Every value already stored in v4 survives the migration unchanged.
    for key in ('character_id', 'persona_id', 'scale', 'screen_id', 'user_name',
                'placement'):
        assert store.settings[key] == v4[key]
    assert store.settings['ai']['temperature'] == 0.3
    assert store.settings['ai']['max_tokens'] == 1200
    assert store.settings['memory']['max_injected'] == 7
    assert store.settings['expression']['style']['warmth'] == 0.2
    assert store.settings['autonomy']['enabled'] is True
    assert store.settings['autonomy']['quiet_start'] == '22:00'
    assert store.settings['autonomy']['quiet_end'] == '07:00'
    assert store.settings['autonomy']['max_per_hour'] == 5
    assert store.settings['growth']['min_sessions'] == 5
    assert store.settings['privacy']['log_prompts'] is True
    assert any('schema version 7' in warning for warning in store.warnings)
    assert json.loads(path.read_text('utf-8')) == store.settings
    assert SettingsStore(tmp_path).warnings == []


def test_v3_document_migrates_to_v7_and_preserves_values(tmp_path):
    v3 = {
        'schema_version': 3, 'character_id': 'godette', 'persona_id': 'godette',
        'scale': 0.75, 'screen_id': 'monitor-2', 'user_name': '小明',
        'placement': {'x': 0.2, 'y': 0.8, 'edge_x': -1, 'edge_y': 1},
        'ai': {
            'provider': 'deepseek', 'base_url': 'https://api.deepseek.com',
            'chat_path': '/v1/chat/completions', 'model': 'deepseek-chat',
            'temperature': 0.3, 'max_tokens': 1200, 'connect_timeout_ms': 5000,
            'request_timeout_ms': 30000, 'max_retries': 1,
            'daily_token_budget': 90000, 'monthly_cost_budget': 9.5,
            'currency': 'USD', 'input_price_per_million': 0.1,
            'output_price_per_million': 0.2,
        },
        'memory': {'enabled': False, 'auto_capture': True, 'max_injected': 7,
                   'max_entry_chars': 800},
        'expression': {
            'enabled': True, 'phrases_enabled': False,
            'style': {'casualness': 0.1, 'warmth': 0.2, 'directness': 0.3,
                      'playfulness': 0.4, 'sentence_length': 0.6,
                      'emoji_frequency': 0.7},
        },
        'autonomy': {'enabled': True, 'quiet_start': '22:00', 'quiet_end': '07:00',
                     'min_seconds_between': 600, 'max_per_hour': 5,
                     'manual_quiet_until': ''},
        'privacy': {'log_prompts': True},
    }
    path = _write(tmp_path, v3)

    store = SettingsStore(tmp_path)
    assert store.settings['schema_version'] == 7
    assert store.settings['scale'] == 0.75
    assert store.settings['ai']['temperature'] == 0.3
    assert store.settings['ai']['embedding'] == AI_EMBEDDING_DEFAULTS
    assert store.settings['memory']['max_injected'] == 7
    assert store.settings['memory']['compaction'] == MEMORY_COMPACTION_DEFAULTS
    assert store.settings['memory']['archive'] == MEMORY_ARCHIVE_DEFAULTS
    assert store.settings['expression']['style']['warmth'] == 0.2
    assert store.settings['autonomy']['enabled'] is True
    assert store.settings['autonomy']['quiet_start'] == '22:00'
    assert store.settings['autonomy']['cloud_enabled'] is True
    assert store.settings['autonomy']['default_quiet_minutes'] == 30
    assert store.settings['growth'] == GROWTH_DEFAULTS
    assert store.settings['desktop'] == DESKTOP_DEFAULTS
    assert store.settings['behavior'] == BEHAVIOR_DEFAULTS
    assert store.settings['performance'] == PERFORMANCE_DEFAULTS
    assert store.settings['privacy']['log_prompts'] is True
    assert any('schema version 7' in warning for warning in store.warnings)
    assert json.loads(path.read_text('utf-8')) == store.settings


def _v5_document(with_voice=True):
    """A schema 5 document, with or without the late-added voice group."""
    document = deepcopy(DEFAULTS)
    document['schema_version'] = 5
    del document['behavior']
    del document['performance']
    del document['bubble']
    del document['greeting']
    del document['autonomy']['default_quiet_minutes']
    if not with_voice:
        del document['voice']
    document['scale'] = 0.8
    document['user_name'] = '阿明'
    document['ai']['temperature'] = 0.25
    document['autonomy']['enabled'] = True
    document['autonomy']['quiet_start'] = '21:30'
    document['autonomy']['max_per_hour'] = 4
    return document


@pytest.mark.parametrize('with_voice', [True, False])
def test_v5_document_migrates_to_v7_and_fills_new_keys(tmp_path, with_voice):
    v5 = _v5_document(with_voice)
    if with_voice:
        v5['voice'] = {'enabled': False, 'voice': 'zh-CN-XiaoxiaoNeural',
                       'rate': 3, 'volume': 40}
    path = _write(tmp_path, v5)

    store = SettingsStore(tmp_path)
    assert store.settings['schema_version'] == 7
    assert store.settings['behavior'] == {'enabled': True}
    assert store.settings['performance'] == {
        'idle_fps': 30, 'drag_fps': 60, 'hidden_fps': 5,
    }
    assert store.settings['autonomy']['default_quiet_minutes'] == 30
    # Everything the v5 file already stored survives the migration.
    assert store.settings['scale'] == 0.8
    assert store.settings['user_name'] == '阿明'
    assert store.settings['ai']['temperature'] == 0.25
    assert store.settings['autonomy']['enabled'] is True
    assert store.settings['autonomy']['quiet_start'] == '21:30'
    assert store.settings['autonomy']['max_per_hour'] == 4
    if with_voice:
        assert store.settings['voice'] == {
            'enabled': False, 'voice': 'zh-CN-XiaoxiaoNeural',
            'rate': 3, 'volume': 40,
        }
    else:
        assert store.settings['voice'] == DEFAULTS['voice']
    assert any('schema version 7' in warning for warning in store.warnings)
    assert json.loads(path.read_text('utf-8')) == store.settings
    # The migrated file is already v7, so a second open has nothing to do.
    assert SettingsStore(tmp_path).warnings == []


def test_v5_with_voice_values_keeps_them_through_migration(tmp_path):
    # The voice group was added to schema 5 by another change; a v5 file that
    # already carries it must not be reset to defaults or treated as invalid.
    v5 = _v5_document(with_voice=True)
    v5['voice']['enabled'] = False
    v5['voice']['volume'] = 0
    _write(tmp_path, v5)

    store = SettingsStore(tmp_path)
    assert store.settings['schema_version'] == 7
    assert store.settings['voice']['enabled'] is False
    assert store.settings['voice']['volume'] == 0
    assert all('Invalid settings' not in warning for warning in store.warnings)


@pytest.mark.parametrize('patch', [
    {'desktop': {'enabled': 1}},
    {'desktop': {'enabled': 'yes'}},
    {'desktop': {'poll_seconds': 0}},
    {'desktop': {'poll_seconds': 61}},
    {'desktop': {'poll_seconds': True}},
    {'desktop': {'poll_seconds': 3.0}},
    {'desktop': {'idle_after_seconds': 29}},
    {'desktop': {'idle_after_seconds': 86401}},
    {'desktop': {'idle_after_seconds': 300.0}},
    {'desktop': {'fullscreen_policy': 'normal'}},
    {'desktop': {'fullscreen_policy': 'gentle'}},
    {'desktop': {'fullscreen_policy': 1}},
    {'desktop': {'dnd_policy': 'normal'}},
    {'desktop': {'dnd_policy': 'gentle'}},
    {'desktop': {'dnd_policy': None}},
    {'desktop': {'work_focus_policy': 'gentle'}},
    {'desktop': {'work_focus_policy': 'loud'}},
    {'desktop': {'idle_policy': 'minimal'}},
    {'desktop': {'idle_policy': 'loud'}},
    {'desktop': {'unknown': True}},
    {'autonomy': {'cloud_enabled': 1}},
    {'autonomy': {'cloud_enabled': 'true'}},
    {'behavior': {'enabled': 1}},
    {'behavior': {'enabled': 'true'}},
    {'behavior': {'enabled': None}},
    {'behavior': {'unknown': True}},
    {'behavior': True},
    {'performance': {'idle_fps': 9}},
    {'performance': {'idle_fps': 61}},
    {'performance': {'idle_fps': 30.0}},
    {'performance': {'idle_fps': True}},
    {'performance': {'drag_fps': 29}},
    {'performance': {'drag_fps': 121}},
    {'performance': {'drag_fps': 60.0}},
    {'performance': {'hidden_fps': 0}},
    {'performance': {'hidden_fps': 16}},
    {'performance': {'hidden_fps': False}},
    {'performance': {'unknown': 1}},
    {'autonomy': {'default_quiet_minutes': 4}},
    {'autonomy': {'default_quiet_minutes': 241}},
    {'autonomy': {'default_quiet_minutes': 30.0}},
    {'autonomy': {'default_quiet_minutes': True}},
    {'autonomy': {'default_quiet_minutes': '30'}},
])
def test_invalid_new_and_desktop_values_are_rejected(tmp_path, patch):
    store = SettingsStore(tmp_path)
    before = store.settings
    raw = (tmp_path / 'settings.json').read_bytes()
    with pytest.raises(ValidationError):
        store.update(patch)
    assert store.settings == before
    assert (tmp_path / 'settings.json').read_bytes() == raw


def test_new_keys_accept_inclusive_boundaries_and_round_trip(tmp_path):
    store = SettingsStore(tmp_path)
    saved = store.update({
        'behavior': {'enabled': False},
        'performance': {'idle_fps': 10, 'drag_fps': 30, 'hidden_fps': 1},
        'autonomy': {'default_quiet_minutes': 5},
    })
    assert saved['behavior']['enabled'] is False
    assert saved['performance'] == {'idle_fps': 10, 'drag_fps': 30, 'hidden_fps': 1}
    assert saved['autonomy']['default_quiet_minutes'] == 5
    assert SettingsStore(tmp_path).settings == saved

    saved = store.update({
        'behavior': {'enabled': True},
        'performance': {'idle_fps': 60, 'drag_fps': 120, 'hidden_fps': 15},
        'autonomy': {'default_quiet_minutes': 240},
    })
    assert saved['behavior']['enabled'] is True
    assert saved['performance'] == {'idle_fps': 60, 'drag_fps': 120, 'hidden_fps': 15}
    assert saved['autonomy']['default_quiet_minutes'] == 240


def test_desktop_boundaries_and_every_policy_choice_are_accepted(tmp_path):
    store = SettingsStore(tmp_path)
    saved = store.update({
        'desktop': {'poll_seconds': 1, 'idle_after_seconds': 30,
                    'fullscreen_policy': 'minimal', 'dnd_policy': 'minimal',
                    'work_focus_policy': 'silent', 'idle_policy': 'silent'},
        'autonomy': {'cloud_enabled': False},
    })
    assert saved['desktop']['poll_seconds'] == 1
    assert saved['desktop']['idle_after_seconds'] == 30
    assert saved['desktop']['fullscreen_policy'] == 'minimal'
    assert saved['desktop']['dnd_policy'] == 'minimal'
    assert saved['desktop']['work_focus_policy'] == 'silent'
    assert saved['desktop']['idle_policy'] == 'silent'
    assert saved['autonomy']['cloud_enabled'] is False

    saved = store.update({
        'desktop': {'poll_seconds': 60, 'idle_after_seconds': 86400,
                    'fullscreen_policy': 'silent', 'dnd_policy': 'silent',
                    'work_focus_policy': 'normal', 'idle_policy': 'normal'},
    })
    assert saved['desktop']['poll_seconds'] == 60
    assert saved['desktop']['idle_after_seconds'] == 86400
    assert saved['desktop']['work_focus_policy'] == 'normal'
    assert saved['desktop']['idle_policy'] == 'normal'
    assert AUTONOMY_DEFAULTS['cloud_enabled'] is True
