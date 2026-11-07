"""State, expression and autonomy budget contracts."""

from datetime import datetime, timedelta, timezone
import json
import random
from types import SimpleNamespace

import pytest

from service.behavior import budget
from service.database import Database
from service.expression import phrases, style
from service.features import autonomy, state as state_feature
from service.settings import SettingsStore
from service.state import character

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def database(tmp_path):
    db = Database(tmp_path / 'pet.db')
    try:
        yield db
    finally:
        db.close()


def _library(tmp_path, entries, name='default.json'):
    path = tmp_path / name
    path.write_text(json.dumps({'version': 1, 'phrases': entries}, ensure_ascii=False),
                    encoding='utf-8')
    return path


def _config(**overrides):
    config = {
        'enabled': True,
        'quiet_start': '23:00',
        'quiet_end': '08:00',
        'min_seconds_between': 0,
        'max_per_hour': 2,
        'manual_quiet_until': '',
    }
    config.update(overrides)
    return config


def test_pad_delta_is_smoothed_clamped_and_decays_toward_baseline(database):
    with database.transaction() as connection:
        state = character.apply_delta(connection, {
            'pad': {'pleasure': 10, 'arousal': -10, 'dominance': 0.5},
            'psych': {'stress': 10, 'energy': -10},
            'personality': {'baseline': {'pleasure': -1}, 'smoothing': 1},
        }, now=NOW)
    assert state['pad']['pleasure'] == 1.0
    assert state['pad']['arousal'] == -1.0
    assert state['psych']['stress'] == 1.0
    assert state['psych']['energy'] == 0.0
    # A model-supplied personality block must never win over the locked values.
    assert state['personality'] == character.PERSONALITY

    peak = state['pad']['pleasure']
    later = NOW + timedelta(seconds=character.PERSONALITY['decay_seconds'])
    with database.transaction() as connection:
        decayed = character.decay(connection, now=later)
    baseline = character.PERSONALITY['baseline']['pleasure']
    assert baseline < decayed['pad']['pleasure'] < peak
    assert decayed['psych']['stress'] < 1.0


def test_stored_personality_is_replaced_by_the_locked_block(database):
    payload = json.dumps({
        'pad': {'pleasure': 0.5, 'arousal': 0.5, 'dominance': 0.5},
        'psych': {'energy': 0.5, 'affinity': 0.5, 'stress': 0.5},
        'personality': {'baseline': {'pleasure': -1}, 'smoothing': 1},
        'updated_at': NOW.isoformat(),
    }, ensure_ascii=False)
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO character_state(subject_user_id, payload, updated_at) VALUES ('owner', ?, ?)",
            (payload, NOW.isoformat()))
    with database.transaction() as connection:
        loaded = character.load_state(connection, now=NOW, apply_decay=False)
    assert loaded['personality'] == character.PERSONALITY


def test_state_commands_get_and_reset(database, tmp_path):
    store = SettingsStore(tmp_path)
    ctx = SimpleNamespace(database=database)
    # ``state.get`` decays toward the baseline using the wall clock, so the
    # delta must be applied now; a pinned NOW would decay to the baseline.
    with database.transaction() as connection:
        character.apply_delta(connection, {'pad': {'pleasure': 1.0}},
                              now=datetime.now(timezone.utc))

    event, payload = state_feature.get({'id': 'r1', 'payload': {}}, store, ctx)
    assert event == 'result'
    assert payload['state']['pad']['pleasure'] > character.PERSONALITY['baseline']['pleasure']
    assert payload['presentation'] == character.presentation(payload['state'])
    assert payload['presentation']['emotion'] == 'happy'

    event, payload = state_feature.reset({'id': 'r2', 'payload': {}}, store, ctx)
    assert event == 'result'
    assert payload['state']['pad'] == character.PERSONALITY['baseline']
    assert payload['state']['psych'] == character.PSYCH_BASELINE
    assert payload['presentation'] == {'emotion': 'neutral', 'weight': 1.0}

    assert state_feature.CAPABILITY['available'] is True
    assert autonomy.CAPABILITY['available'] is True


def test_default_phrase_library_loads():
    items = phrases.load_phrases()
    assert items
    assert all(item['text'].strip() for item in items)


def test_phrase_cooldown_is_enforced(database, tmp_path):
    default = _library(tmp_path, [{
        'id': 'only', 'text': '你好', 'category': 'idle',
        'cooldown_seconds': 60, 'daily_limit': 3,
    }])
    user = tmp_path / 'user.json'
    with database.transaction() as connection:
        first = phrases.select_phrase(connection, category='idle', now=NOW,
                                      default_path=default, user_path=user)
        assert first['id'] == 'only'
        blocked = phrases.select_phrase(connection, category='idle',
                                        now=NOW + timedelta(seconds=59),
                                        default_path=default, user_path=user)
        assert blocked is None
        again = phrases.select_phrase(connection, category='idle',
                                      now=NOW + timedelta(seconds=60),
                                      default_path=default, user_path=user)
        assert again['id'] == 'only'


def test_phrase_daily_limit_and_user_disabled_phrase(database, tmp_path):
    default = _library(tmp_path, [
        {'id': 'a', 'text': '甲', 'category': 'idle', 'cooldown_seconds': 0, 'daily_limit': 1},
        {'id': 'b', 'text': '乙', 'category': 'idle', 'cooldown_seconds': 0, 'daily_limit': 1},
    ])
    user = tmp_path / 'user.json'
    user.write_text(json.dumps({'disabled': ['b']}, ensure_ascii=False), encoding='utf-8')
    with database.transaction() as connection:
        assert phrases.select_phrase(connection, category='idle', now=NOW,
                                     default_path=default, user_path=user)['id'] == 'a'
        # 'a' reached its daily limit and 'b' is disabled, so nothing is usable today.
        assert phrases.select_phrase(connection, category='idle',
                                     now=NOW + timedelta(minutes=1),
                                     default_path=default, user_path=user) is None
        tomorrow = phrases.select_phrase(connection, category='idle',
                                         now=NOW + timedelta(days=1),
                                         default_path=default, user_path=user)
        assert tomorrow['id'] == 'a'


def test_style_only_deletes_redundancy_and_keeps_negation():
    assert style.polish('好的，今天下雨了。') == '今天下雨了。'
    assert style.polish('嗯，嗯，我在。') == '我在。'
    assert style.polish('今天下雨了。今天下雨了。记得带伞。') == '今天下雨了。记得带伞。'
    assert style.polish('今天\n\n\n\n不出门。') == '今天\n\n不出门。'
    assert style.polish('我不去，也不想去。') == '我不去，也不想去。'


def test_quiet_hours_and_manual_quiet(database):
    assert budget.can_speak(
        database, _config(), now=datetime(2026, 11, 4, 23, 30, tzinfo=timezone.utc)
    )['reason'] == 'quiet_hours'
    assert budget.can_speak(
        database, _config(), now=datetime(2026, 11, 4, 7, 59, tzinfo=timezone.utc)
    )['reason'] == 'quiet_hours'
    assert budget.can_speak(
        database, _config(), now=datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
    )['allowed'] is True

    manual = _config(manual_quiet_until=(NOW + timedelta(minutes=30)).isoformat())
    assert budget.can_speak(database, manual, now=NOW)['reason'] == 'manual_quiet'
    with database.transaction() as connection:
        budget.set_manual_quiet(connection, NOW + timedelta(minutes=10))
    assert budget.can_speak(database, _config(), now=NOW)['reason'] == 'manual_quiet'
    assert budget.can_speak(database, _config(),
                            now=NOW + timedelta(minutes=11))['allowed'] is True


def test_minimum_interval_and_hourly_cap(database):
    config = _config(quiet_start='00:00', quiet_end='00:00',
                     min_seconds_between=600, max_per_hour=2)
    with database.transaction() as connection:
        assert budget.can_speak(connection, config, now=NOW)['allowed'] is True
        budget.record_autonomous(connection, now=NOW)
    assert budget.can_speak(database, config,
                            now=NOW + timedelta(seconds=599))['reason'] == 'min_interval'
    with database.transaction() as connection:
        assert budget.can_speak(connection, config,
                                now=NOW + timedelta(seconds=600))['allowed'] is True
        budget.record_autonomous(connection, now=NOW + timedelta(seconds=600))
    decision = budget.can_speak(database, config, now=NOW + timedelta(seconds=1200))
    assert decision['reason'] == 'hourly_limit'
    assert decision['hourly_count'] == 2
    assert budget.can_speak(database, config,
                            now=NOW + timedelta(hours=1))['allowed'] is True


def test_ignored_message_downgrades_frequency_until_user_activity(database):
    config = _config(quiet_start='00:00', quiet_end='00:00',
                     min_seconds_between=900, max_per_hour=4)
    with database.transaction() as connection:
        budget.record_autonomous(connection, now=NOW)
    decision = budget.can_speak(database, config, now=NOW + timedelta(seconds=1200))
    assert decision['ignored'] is True
    assert decision['effective_min_seconds'] == 1800
    assert decision['reason'] == 'min_interval'

    with database.transaction() as connection:
        budget.note_user_activity(connection, now=NOW + timedelta(seconds=1250))
    recovered = budget.can_speak(database, config, now=NOW + timedelta(seconds=1250))
    assert recovered['ignored'] is False
    assert recovered['allowed'] is True


def test_tick_is_local_and_respects_the_budget(database, tmp_path):
    store = SettingsStore(tmp_path)
    assert autonomy.tick(store, database, now=NOW) is None

    store.update({'autonomy': {
        'enabled': True, 'quiet_start': '00:00', 'quiet_end': '00:00',
        'min_seconds_between': 0, 'max_per_hour': 1,
    }})
    suggestion = autonomy.tick(store, database, now=NOW, rng=random.Random(0))
    assert suggestion is not None
    assert suggestion['text']
    assert suggestion['category'] == 'idle'
    assert autonomy.tick(store, database, now=NOW + timedelta(seconds=1),
                         rng=random.Random(0)) is None


def test_tick_accepts_runtime_monotonic_timestamp(database, tmp_path):
    store = SettingsStore(tmp_path)
    store.update({'autonomy': {
        'enabled': True, 'quiet_start': '00:00', 'quiet_end': '00:00',
        'min_seconds_between': 0, 'max_per_hour': 1,
    }})
    suggestion = autonomy.tick(store, database, now=12345.0, rng=random.Random(0))
    assert suggestion is not None
    assert suggestion['text']


def test_runtime_registration_posts_one_bubble_per_budget_hour(database, tmp_path):
    store = SettingsStore(tmp_path)
    store.update({'autonomy': {
        'enabled': True, 'quiet_start': '00:00', 'quiet_end': '00:00',
        'min_seconds_between': 0, 'max_per_hour': 1,
    }})
    posted = []
    hooks = []

    class _Runtime:
        def register_tick(self, hook):
            hooks.append(hook)

        def post(self, request_id, event, payload):
            posted.append((request_id, event, payload))

    runtime = _Runtime()
    hook = autonomy.register_tick(runtime, store, database, interval_seconds=30)
    assert hooks == [hook]
    hook(100.0)
    hook(110.0)
    assert len(posted) == 1
    assert posted[0][1] == autonomy.BUBBLE_EVENT
    assert posted[0][2]['text']
    hook(200.0)
    assert len(posted) == 1


def test_autonomy_commands_update_and_set_quiet(database, tmp_path):
    store = SettingsStore(tmp_path)
    ctx = SimpleNamespace(database=database)
    event, payload = autonomy.set_quiet(
        {'id': 'q', 'payload': {'minutes': 15}}, store, ctx)
    assert event == 'result'
    assert payload['manual_quiet_until']
    store.update({'autonomy': {'enabled': True}})
    assert budget.can_speak(database, store.settings['autonomy'])['reason'] == 'manual_quiet'

    event, payload = autonomy.update(
        {'id': 'u', 'payload': {'patch': {'enabled': True, 'max_per_hour': 3}}},
        store, ctx)
    assert event == 'result'
    assert payload['autonomy']['enabled'] is True
    assert payload['autonomy']['max_per_hour'] == 3

    autonomy.set_quiet({'id': 'c', 'payload': {'minutes': 0}}, store, ctx)
    assert store.settings['autonomy']['manual_quiet_until'] == ''
