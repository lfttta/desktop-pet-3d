import json
from pathlib import Path

import pytest


def test_defaults_persist_and_returned_values_are_detached(tmp_path):
    from service.settings import DEFAULTS, SettingsStore
    store = SettingsStore(tmp_path)
    assert store.settings == DEFAULTS
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


def test_schema6_defaults_include_phase2_desktop_behavior_and_performance():
    from service.settings import (AI_EMBEDDING_DEFAULTS, AUTONOMY_DEFAULTS,
                                  BEHAVIOR_DEFAULTS, DEFAULTS, DESKTOP_DEFAULTS,
                                  GROWTH_DEFAULTS, MEMORY_ARCHIVE_DEFAULTS,
                                  MEMORY_COMPACTION_DEFAULTS, PERFORMANCE_DEFAULTS,
                                  SCHEMA_VERSION)
    assert SCHEMA_VERSION == 6
    assert DEFAULTS['schema_version'] == 6
    assert DEFAULTS['memory']['compaction'] == {
        'enabled': False, 'model_compress': False, 'daily_limit': 20,
        'min_age_days': 7, 'max_per_run': 10, 'protect_importance': 0.8,
    }
    assert DEFAULTS['memory']['compaction'] == MEMORY_COMPACTION_DEFAULTS
    assert DEFAULTS['memory']['archive'] == MEMORY_ARCHIVE_DEFAULTS
    assert DEFAULTS['ai']['embedding'] == {
        'enabled': False, 'model': '', 'dimensions': 0,
    }
    assert DEFAULTS['ai']['embedding'] == AI_EMBEDDING_DEFAULTS
    assert DEFAULTS['growth'] == GROWTH_DEFAULTS
    assert DEFAULTS['desktop'] == {
        'enabled': True, 'poll_seconds': 3, 'idle_after_seconds': 300,
        'fullscreen_policy': 'silent', 'dnd_policy': 'silent',
        'work_focus_policy': 'minimal', 'idle_policy': 'gentle',
    }
    assert DEFAULTS['desktop'] == DESKTOP_DEFAULTS
    assert DEFAULTS['behavior'] == {'enabled': True}
    assert DEFAULTS['behavior'] == BEHAVIOR_DEFAULTS
    assert DEFAULTS['performance'] == {
        'idle_fps': 30, 'drag_fps': 60, 'hidden_fps': 5,
    }
    assert DEFAULTS['performance'] == PERFORMANCE_DEFAULTS
    assert DEFAULTS['autonomy'] == AUTONOMY_DEFAULTS
    assert DEFAULTS['autonomy']['cloud_enabled'] is True
    assert DEFAULTS['autonomy']['default_quiet_minutes'] == 30


def test_v3_document_migrates_to_v6_and_preserves_values(tmp_path):
    from service.settings import (AI_EMBEDDING_DEFAULTS, BEHAVIOR_DEFAULTS,
                                  DESKTOP_DEFAULTS, GROWTH_DEFAULTS,
                                  MEMORY_ARCHIVE_DEFAULTS, MEMORY_COMPACTION_DEFAULTS,
                                  PERFORMANCE_DEFAULTS, SettingsStore)
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
    path = tmp_path / 'settings.json'
    path.write_bytes((json.dumps(v3, ensure_ascii=False) + '\n').encode('utf-8'))
    store = SettingsStore(tmp_path)
    assert store.settings['schema_version'] == 6
    for key in ('character_id', 'persona_id', 'scale', 'screen_id', 'user_name',
                'placement'):
        assert store.settings[key] == v3[key]
    assert store.settings['ai']['temperature'] == 0.3
    assert store.settings['ai']['max_tokens'] == 1200
    assert store.settings['ai']['embedding'] == AI_EMBEDDING_DEFAULTS
    assert store.settings['memory']['enabled'] is False
    assert store.settings['memory']['max_injected'] == 7
    assert store.settings['memory']['compaction'] == MEMORY_COMPACTION_DEFAULTS
    assert store.settings['memory']['archive'] == MEMORY_ARCHIVE_DEFAULTS
    assert store.settings['expression']['phrases_enabled'] is False
    assert store.settings['expression']['style']['warmth'] == 0.2
    assert store.settings['autonomy']['enabled'] is True
    assert store.settings['autonomy']['quiet_start'] == '22:00'
    assert store.settings['privacy']['log_prompts'] is True
    assert store.settings['growth'] == GROWTH_DEFAULTS
    assert store.settings['desktop'] == DESKTOP_DEFAULTS
    assert store.settings['behavior'] == BEHAVIOR_DEFAULTS
    assert store.settings['performance'] == PERFORMANCE_DEFAULTS
    assert store.settings['autonomy']['cloud_enabled'] is True
    assert store.settings['autonomy']['default_quiet_minutes'] == 30
    assert any('schema version 6' in warning for warning in store.warnings)
    assert json.loads(path.read_text('utf-8')) == store.settings
    assert SettingsStore(tmp_path).warnings == []


def test_nested_group_patch_deep_merges_siblings(tmp_path):
    from service.settings import (MEMORY_ARCHIVE_DEFAULTS, MEMORY_COMPACTION_DEFAULTS,
                                  SettingsStore)
    store = SettingsStore(tmp_path)
    store.update({'memory': {'compaction': {'model_compress': True}}})
    memory = store.settings['memory']
    assert memory['compaction']['model_compress'] is True
    assert memory['compaction']['daily_limit'] == 20
    assert memory['compaction']['max_per_run'] == 10
    assert memory['archive'] == MEMORY_ARCHIVE_DEFAULTS
    assert memory['enabled'] is True
    assert memory['max_injected'] == 5

    store.update({'memory': {'max_injected': 3,
                             'compaction': {'daily_limit': 2}},
                  'expression': {'style': {'warmth': 0.9}}})
    memory = store.settings['memory']
    assert memory['max_injected'] == 3
    assert memory['compaction']['daily_limit'] == 2
    assert memory['compaction']['model_compress'] is True
    assert store.settings['expression']['style']['warmth'] == 0.9
    assert store.settings['expression']['style']['casualness'] == 0.5


@pytest.mark.parametrize('patch', [
    {'memory': {'compaction': {'daily_limit': -1}}},
    {'memory': {'compaction': {'daily_limit': 10001}}},
    {'memory': {'compaction': {'daily_limit': True}}},
    {'memory': {'compaction': {'min_age_days': -1}}},
    {'memory': {'compaction': {'min_age_days': 3651}}},
    {'memory': {'compaction': {'max_per_run': 0}}},
    {'memory': {'compaction': {'max_per_run': 101}}},
    {'memory': {'compaction': {'protect_importance': -0.1}}},
    {'memory': {'compaction': {'protect_importance': 1.1}}},
    {'memory': {'compaction': {'unknown': 1}}},
    {'memory': {'archive': {'topic_stale_days': 0}}},
    {'memory': {'archive': {'topic_stale_days': 3651}}},
    {'memory': {'archive': {'enabled': 1}}},
    {'ai': {'embedding': {'dimensions': -1}}},
    {'ai': {'embedding': {'dimensions': 100001}}},
    {'ai': {'embedding': {'model': 1}}},
    {'ai': {'embedding': {'unknown': 1}}},
    {'growth': {'min_sessions': 0}},
    {'growth': {'min_sessions': 1001}},
    {'growth': {'min_sessions': True}},
    {'growth': {'min_memories': 0}},
    {'growth': {'min_memories': 1001}},
    {'growth': {'max_step': 0.0009}},
    {'growth': {'max_step': 0.21}},
    {'growth': {'cooldown_days': -1}},
    {'growth': {'cooldown_days': 366}},
    {'growth': {'locked_traits': 'warmth'}},
    {'growth': {'locked_traits': ['']}},
    {'growth': {'enabled': 1}},
    {'growth': {'unknown': 1}},
])
def test_v4_range_and_type_validation_rejects_out_of_bounds(tmp_path, patch):
    from service.settings import SettingsStore, ValidationError
    store = SettingsStore(tmp_path)
    before = store.settings
    raw = (tmp_path / 'settings.json').read_bytes()
    with pytest.raises(ValidationError):
        store.update(patch)
    assert store.settings == before
    assert (tmp_path / 'settings.json').read_bytes() == raw


def test_v4_accepts_inclusive_boundaries(tmp_path):
    from service.settings import SettingsStore
    store = SettingsStore(tmp_path)
    saved = store.update({
        'memory': {'compaction': {'daily_limit': 0, 'min_age_days': 0,
                                  'max_per_run': 1, 'protect_importance': 0},
                   'archive': {'topic_stale_days': 1}},
        'ai': {'embedding': {'dimensions': 0}},
        'growth': {'min_sessions': 1, 'min_memories': 1, 'max_step': 0.001,
                   'cooldown_days': 0, 'locked_traits': ['warmth']},
    })
    assert saved['memory']['compaction']['daily_limit'] == 0
    assert saved['memory']['archive']['topic_stale_days'] == 1
    saved = store.update({
        'memory': {'compaction': {'daily_limit': 10000, 'min_age_days': 3650,
                                  'max_per_run': 100, 'protect_importance': 1},
                   'archive': {'topic_stale_days': 3650}},
        'ai': {'embedding': {'dimensions': 4096}},
        'growth': {'min_sessions': 1000, 'min_memories': 1000, 'max_step': 0.2,
                   'cooldown_days': 365},
    })
    assert saved['memory']['compaction']['max_per_run'] == 100
    assert saved['ai']['embedding']['dimensions'] == 4096
    assert saved['growth']['max_step'] == 0.2
