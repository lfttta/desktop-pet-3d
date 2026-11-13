"""Schema v7 bubble/greeting settings and time-of-day phrase selection."""

from copy import deepcopy
from datetime import datetime, timezone
import json

import pytest

from service.database import Database
from service.expression import phrases
from service.settings import (
    BUBBLE_DEFAULTS,
    DEFAULTS,
    GREETING_DEFAULTS,
    SCHEMA_VERSION,
    SettingsStore,
    ValidationError,
    validate_document,
)


def _v6_document():
    """A schema 6 document: every v6 group, neither bubble, greeting nor hotkeys."""
    document = deepcopy(DEFAULTS)
    document['schema_version'] = 6
    del document['bubble']
    del document['greeting']
    del document['hotkeys']
    # The v9 tone did not exist in v6; the migration must backfill it.
    del document['expression']['tone']
    # The v11 vision group did not exist in v6 either.
    del document['vision']
    document['scale'] = 0.85
    document['user_name'] = '小周'
    document['expression']['style']['warmth'] = 0.3
    document['behavior'] = {'enabled': False}
    document['performance']['idle_fps'] = 20
    return document


def _write(tmp_path, document):
    path = tmp_path / 'settings.json'
    path.write_bytes((json.dumps(document, ensure_ascii=False) + '\n').encode('utf-8'))
    return path


def test_v9_defaults_keep_bubble_and_greeting_groups():
    assert SCHEMA_VERSION == 11
    assert DEFAULTS['bubble'] == {
        'enabled': True, 'duration_seconds': 4, 'show_replies': True,
    }
    assert DEFAULTS['bubble'] == BUBBLE_DEFAULTS
    assert DEFAULTS['greeting'] == {
        'enabled': True, 'on_start': True, 'cooldown_minutes': 60,
    }
    assert DEFAULTS['greeting'] == GREETING_DEFAULTS
    assert validate_document(deepcopy(DEFAULTS)) == DEFAULTS


def test_v6_document_gains_bubble_and_greeting_and_keeps_values(tmp_path):
    v6 = _v6_document()
    path = _write(tmp_path, v6)

    store = SettingsStore(tmp_path)
    assert store.settings['schema_version'] == SCHEMA_VERSION
    assert store.settings['bubble'] == BUBBLE_DEFAULTS
    assert store.settings['greeting'] == GREETING_DEFAULTS
    assert store.settings['hotkeys'] == DEFAULTS['hotkeys']
    assert store.settings['expression']['tone'] == 'godette'
    # Every value the v6 file already stored survives unchanged.
    assert store.settings['scale'] == 0.85
    assert store.settings['user_name'] == '小周'
    assert store.settings['expression']['style']['warmth'] == 0.3
    assert store.settings['behavior'] == {'enabled': False, 'walk_enabled': True}
    assert store.settings['performance']['idle_fps'] == 20
    assert any(f'schema version {SCHEMA_VERSION}' in warning for warning in store.warnings)
    assert json.loads(path.read_text('utf-8')) == store.settings
    # The migrated file is already v7, so a second open has nothing to do.
    assert SettingsStore(tmp_path).warnings == []


@pytest.mark.parametrize('patch', [
    {'bubble': {'duration_seconds': 1}},
    {'bubble': {'duration_seconds': 16}},
    {'bubble': {'duration_seconds': 4.0}},
    {'bubble': {'duration_seconds': True}},
    {'bubble': {'duration_seconds': '4'}},
    {'bubble': {'enabled': 1}},
    {'bubble': {'show_replies': None}},
    {'bubble': {'unknown': True}},
    {'greeting': {'cooldown_minutes': 4}},
    {'greeting': {'cooldown_minutes': 1441}},
    {'greeting': {'cooldown_minutes': 60.0}},
    {'greeting': {'cooldown_minutes': True}},
    {'greeting': {'on_start': 'yes'}},
    {'greeting': {'unknown': True}},
])
def test_out_of_range_bubble_and_greeting_values_are_rejected(tmp_path, patch):
    store = SettingsStore(tmp_path)
    before = store.settings
    raw = (tmp_path / 'settings.json').read_bytes()
    with pytest.raises(ValidationError):
        store.update(patch)
    assert store.settings == before
    assert (tmp_path / 'settings.json').read_bytes() == raw


def test_bubble_and_greeting_accept_inclusive_bounds_and_merge_partially(tmp_path):
    store = SettingsStore(tmp_path)
    saved = store.update({
        'bubble': {'duration_seconds': 2},
        'greeting': {'cooldown_minutes': 5},
    })
    assert saved['bubble'] == {
        'enabled': True, 'duration_seconds': 2, 'show_replies': True,
    }
    assert saved['greeting'] == {
        'enabled': True, 'on_start': True, 'cooldown_minutes': 5,
    }

    saved = store.update({
        'bubble': {'duration_seconds': 15, 'show_replies': False},
        'greeting': {'cooldown_minutes': 1440, 'on_start': False},
    })
    assert saved['bubble'] == {
        'enabled': True, 'duration_seconds': 15, 'show_replies': False,
    }
    assert saved['greeting'] == {
        'enabled': True, 'on_start': False, 'cooldown_minutes': 1440,
    }
    assert SettingsStore(tmp_path).settings == saved


@pytest.fixture
def database(tmp_path):
    db = Database(tmp_path / 'pet.db')
    try:
        yield db
    finally:
        db.close()


def _library(tmp_path, entries):
    path = tmp_path / 'phrases.json'
    path.write_text(json.dumps({'version': 1, 'phrases': entries}, ensure_ascii=False),
                    encoding='utf-8')
    return path


def _window_phrases():
    return [
        {'id': 'w_morning', 'text': '早上的台词。', 'category': 'care',
         'cooldown_seconds': 0, 'daily_limit': 0, 'time_window': 'morning'},
        {'id': 'w_afternoon', 'text': '下午的台词。', 'category': 'care',
         'cooldown_seconds': 0, 'daily_limit': 0, 'time_window': 'afternoon'},
        {'id': 'w_evening', 'text': '傍晚的台词。', 'category': 'care',
         'cooldown_seconds': 0, 'daily_limit': 0, 'time_window': 'evening'},
        {'id': 'w_night', 'text': '夜里的台词。', 'category': 'care',
         'cooldown_seconds': 0, 'daily_limit': 0, 'time_window': 'night'},
    ]


WINDOW_BY_ID = {
    'w_morning': 'morning',
    'w_afternoon': 'afternoon',
    'w_evening': 'evening',
    'w_night': 'night',
}


@pytest.mark.parametrize('hour, expected', [
    (8, 'w_morning'),
    (14, 'w_afternoon'),
    (19, 'w_evening'),
    (23, 'w_night'),
])
def test_select_phrase_filters_candidates_by_time_window(database, tmp_path, hour, expected):
    default = _library(tmp_path, _window_phrases())
    user = tmp_path / 'user.json'
    now = datetime(2026, 11, 4, hour, 0, tzinfo=timezone.utc)

    with database.transaction() as connection:
        picked = phrases.select_phrase(connection, now=now, default_path=default,
                                       user_path=user)

    assert picked is not None
    assert picked['id'] == expected
    assert picked['time_window'] == WINDOW_BY_ID[expected]
    for other in WINDOW_BY_ID:
        if other != expected:
            assert picked['id'] != other


@pytest.mark.parametrize('hour, expected', [
    (0, 'w_night'),
    (4, 'w_night'),
    (5, 'w_morning'),
    (10, 'w_morning'),
    (11, 'w_afternoon'),
    (16, 'w_afternoon'),
    (17, 'w_evening'),
    (21, 'w_evening'),
    (22, 'w_night'),
])
def test_window_boundaries_match_the_documented_hours(database, tmp_path, hour, expected):
    default = _library(tmp_path, _window_phrases())
    user = tmp_path / 'user.json'
    now = datetime(2026, 11, 4, hour, 30, tzinfo=timezone.utc)

    with database.transaction() as connection:
        picked = phrases.select_phrase(connection, now=now, default_path=default,
                                       user_path=user)

    assert picked['id'] == expected


def test_any_phrase_stays_available_in_every_window(database, tmp_path):
    default = _library(tmp_path, [{
        'id': 'w_any', 'text': '随时都在。', 'category': 'care',
        'cooldown_seconds': 0, 'daily_limit': 0, 'time_window': 'any',
    }])
    user = tmp_path / 'user.json'

    for hour in (8, 14, 19, 23):
        now = datetime(2026, 11, 4, hour, 0, tzinfo=timezone.utc)
        with database.transaction() as connection:
            chosen = phrases.select_phrase(connection, now=now,
                                           default_path=default, user_path=user)
        assert chosen['id'] == 'w_any'
        assert chosen['time_window'] == 'any'


def test_missing_time_window_is_treated_as_any(database, tmp_path):
    default = _library(tmp_path, [{
        'id': 'plain', 'text': '没有时段的旧台词。', 'category': 'care',
        'cooldown_seconds': 0, 'daily_limit': 0,
    }])
    user = tmp_path / 'user.json'
    now = datetime(2026, 11, 4, 3, 0, tzinfo=timezone.utc)

    with database.transaction() as connection:
        picked = phrases.select_phrase(connection, now=now,
                                       default_path=default, user_path=user)

    assert picked['id'] == 'plain'
    assert picked['time_window'] == 'any'


def test_now_none_does_not_filter_by_time_window(database, tmp_path):
    default = _library(tmp_path, [{
        'id': 'only_morning', 'text': '只有早上出现。', 'category': 'care',
        'cooldown_seconds': 0, 'daily_limit': 0, 'time_window': 'morning',
    }])
    user = tmp_path / 'user.json'
    afternoon = datetime(2026, 11, 4, 14, 0, tzinfo=timezone.utc)

    with database.transaction() as connection:
        # 14:00 hides the morning-only phrase when ``now`` is supplied.
        assert phrases.select_phrase(connection, now=afternoon,
                                     default_path=default, user_path=user) is None
        # Without ``now`` the old unfiltered behaviour is kept.
        unfiltered = phrases.select_phrase(connection, default_path=default,
                                           user_path=user)

    assert unfiltered['id'] == 'only_morning'


def test_default_library_loads_with_a_valid_window_on_every_phrase():
    items = phrases.load_phrases()
    assert len(items) >= 12
    ids = [item['id'] for item in items]
    assert len(ids) == len(set(ids))

    valid = set(phrases.TIME_WINDOW_CHOICES)
    for item in items:
        assert item['text'].strip()
        assert item['time_window'] in valid

    by_id = {item['id']: item for item in items}
    assert by_id['greet_morning']['time_window'] == 'morning'
    assert by_id['greet_evening']['time_window'] == 'evening'
    assert by_id['night_rest']['time_window'] == 'night'
    # The other phrases that predate time windows stay usable at every hour.
    for phrase_id in ('idle_presence', 'idle_water', 'idle_stretch',
                      'idle_smalltalk', 'idle_encourage', 'care_rest',
                      'care_eyes'):
        assert by_id[phrase_id]['time_window'] == 'any'

    # The library covers all four windows, so narrowed selection still has lines.
    assert {'morning', 'afternoon', 'evening', 'night'} <= {
        item['time_window'] for item in items}
