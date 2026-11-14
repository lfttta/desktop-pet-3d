"""Content and selection contracts for the shipped Godette phrase library.

The library is data, not code, so its rules are checked directly: volume,
Chinese-only 8-24 character lines, unique ids and texts, a per-category floor,
cooldown/daily-limit ranges, coverage of every time window, and the category
whitelist the selector enforces.
"""

from collections import Counter
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re

import pytest

from service.database import Database
from service.expression import phrases

LIBRARY_PATH = Path(__file__).resolve().parents[2] / 'data' / 'expression' / 'phrases.json'

# Intents the product asks for. Every one needs a usable pool, not one line.
REQUIRED_CATEGORIES = ('greeting', 'care', 'idle', 'night', 'happy', 'sad',
                       'apology', 'waiting', 'celebration', 'tired')
MIN_PER_CATEGORY = 10
MIN_NIGHT = 12
MIN_TOTAL = 150
MIN_PER_WINDOW = 10
TEXT_MIN_CHARS = 8
TEXT_MAX_CHARS = 24
HANZI = re.compile(r'[\u4e00-\u9fff]')
LATIN = re.compile(r'[A-Za-z0-9]')


def _document():
    return json.loads(LIBRARY_PATH.read_text(encoding='utf-8'))


def _entries():
    document = _document()
    assert isinstance(document, dict)
    assert isinstance(document.get('phrases'), list)
    return document['phrases']


@pytest.fixture
def database(tmp_path):
    db = Database(tmp_path / 'pet.db')
    try:
        yield db
    finally:
        db.close()


def _library(tmp_path, entries, name='phrases.json'):
    path = tmp_path / name
    path.write_text(json.dumps({'version': 1, 'phrases': entries}, ensure_ascii=False),
                    encoding='utf-8')
    return path


def test_library_has_at_least_150_phrases():
    entries = _entries()
    assert len(entries) >= MIN_TOTAL, 'the shipped library needs at least %d phrases' % MIN_TOTAL


def test_every_phrase_is_chinese_and_8_to_24_characters():
    problems = []
    for entry in _entries():
        text = entry.get('text')
        if type(text) is not str or not text.strip():
            problems.append('%s: empty text' % entry.get('id'))
            continue
        stripped = text.strip()
        if stripped != text:
            problems.append('%s: surrounding whitespace' % entry.get('id'))
        if not TEXT_MIN_CHARS <= len(stripped) <= TEXT_MAX_CHARS:
            problems.append('%s: %d chars' % (entry.get('id'), len(stripped)))
        if not HANZI.search(stripped):
            problems.append('%s: no Chinese characters' % entry.get('id'))
        if LATIN.search(stripped):
            problems.append('%s: contains latin letters or digits' % entry.get('id'))
    assert problems == []


def test_ids_and_texts_are_unique():
    entries = _entries()
    ids = [entry.get('id') for entry in entries]
    texts = [entry.get('text') for entry in entries]
    assert len(ids) == len(set(ids)), 'duplicate ids: %s' % [
        value for value, count in Counter(ids).items() if count > 1]
    assert len(texts) == len(set(texts)), 'duplicate texts: %s' % [
        value for value, count in Counter(texts).items() if count > 1]


def test_required_categories_have_enough_lines():
    counts = Counter(entry.get('category') for entry in _entries())
    missing = [name for name in REQUIRED_CATEGORIES if counts[name] < MIN_PER_CATEGORY]
    assert missing == [], 'categories under %d lines: %s' % (
        MIN_PER_CATEGORY, {name: counts[name] for name in missing})
    assert counts['night'] >= MIN_NIGHT, 'night has only %d lines' % counts['night']


def test_every_category_is_on_the_whitelist():
    known = set(phrases.CATEGORY_NAMES)
    unknown = sorted({str(entry.get('category')) for entry in _entries()} - known)
    assert unknown == [], 'categories outside the whitelist: %s' % unknown


def test_every_time_window_has_enough_lines():
    counts = Counter(entry.get('time_window') for entry in _entries())
    valid = set(phrases.TIME_WINDOW_CHOICES)
    assert set(counts) <= valid, 'unknown windows: %s' % (set(counts) - valid)
    short = {name: counts[name] for name in valid if counts[name] < MIN_PER_WINDOW}
    assert short == {}, 'windows under %d lines: %s' % (MIN_PER_WINDOW, short)


def test_cooldowns_and_daily_limits_stay_in_range():
    problems = []
    for entry in _entries():
        cooldown = entry.get('cooldown_seconds')
        daily = entry.get('daily_limit')
        if type(cooldown) is not int or not 2 * 3600 <= cooldown <= 6 * 3600:
            problems.append('%s: cooldown %r' % (entry.get('id'), cooldown))
        if type(daily) is not int or daily not in (1, 2):
            problems.append('%s: daily_limit %r' % (entry.get('id'), daily))
        if entry.get('enabled') is not True:
            problems.append('%s: not enabled' % entry.get('id'))
    assert problems == []


def test_click_pools_cover_every_hour():
    """greeting/care and happy/sad must have a line at each wall-clock hour."""
    entries = _entries()
    windows = set(phrases.TIME_WINDOW_CHOICES)

    def texts_for(categories, window):
        return [entry['text'] for entry in entries
                if entry.get('category') in categories
                and entry.get('time_window') in (window, 'any')]

    for hour in range(24):
        window = phrases.time_window_for(
            datetime(2026, 11, 4, hour, 0, tzinfo=timezone.utc))
        assert window in windows
        assert texts_for(('greeting', 'care'), window), 'no click line at %02d:00' % hour
        assert texts_for(('happy',), window), 'no happy click line at %02d:00' % hour
        assert texts_for(('sad',), window), 'no sad click line at %02d:00' % hour
        assert texts_for(('greeting',), window), 'no start greeting at %02d:00' % hour
        assert texts_for(('apology',), window), 'no apology line at %02d:00' % hour
        assert texts_for(('waiting',), window), 'no waiting line at %02d:00' % hour


def test_whitelist_covers_the_legacy_general_bucket():
    assert 'general' in phrases.CATEGORY_NAMES
    assert phrases.DEFAULT_CATEGORY in phrases.CATEGORY_NAMES
    assert phrases.category_choices() == list(phrases.PHRASE_CATEGORIES)


@pytest.mark.parametrize('value', [None, '', 'general', 'happy', 'waiting'])
def test_normalize_category_accepts_whitelisted_names(value):
    expected = 'general' if value in (None, '') else value
    assert phrases.normalize_category(value) == expected


@pytest.mark.parametrize('value', ['cheer', 'Happy', ' greeting', 0, True, [], {}])
def test_normalize_category_rejects_unknown_names(value):
    assert phrases.normalize_category(value) is None


def test_unknown_category_is_rejected_at_load(tmp_path):
    path = _library(tmp_path, [
        {'id': 'good', 'text': '这句话是合法的。', 'category': 'care',
         'cooldown_seconds': 7200, 'daily_limit': 1},
        {'id': 'typo', 'text': '这句话分类写错了。', 'category': 'cheer',
         'cooldown_seconds': 7200, 'daily_limit': 1},
    ])
    items = phrases.load_phrases(default_path=path, user_path=tmp_path / 'user.json')
    assert [item['id'] for item in items] == ['good']


def test_missing_category_is_kept_as_general(tmp_path):
    path = _library(tmp_path, [
        {'id': 'plain', 'text': '没有写分类的旧台词。',
         'cooldown_seconds': 7200, 'daily_limit': 1},
    ])
    items = phrases.load_phrases(default_path=path, user_path=tmp_path / 'user.json')
    assert items[0]['category'] == 'general'


def test_unknown_override_category_does_not_relabel_a_phrase(tmp_path):
    path = _library(tmp_path, [
        {'id': 'care_a', 'text': '记得喝点水。', 'category': 'care',
         'cooldown_seconds': 7200, 'daily_limit': 1},
    ])
    user = tmp_path / 'user.json'
    user.write_text(json.dumps({'phrases': [
        {'id': 'care_a', 'category': 'cheer'},
        {'id': 'care_a', 'category': ''},
    ]}, ensure_ascii=False), encoding='utf-8')
    items = phrases.load_phrases(default_path=path, user_path=user)
    assert items[0]['category'] == 'care'


def test_whitelisted_override_category_does_relabel_a_phrase(tmp_path):
    """The override is still allowed to move a phrase between known intents."""
    path = _library(tmp_path, [
        {'id': 'care_a', 'text': '记得喝点水。', 'category': 'care',
         'cooldown_seconds': 7200, 'daily_limit': 1},
    ])
    user = tmp_path / 'user.json'
    user.write_text(json.dumps({'phrases': [
        {'id': 'care_a', 'category': 'tired'},
    ]}, ensure_ascii=False), encoding='utf-8')
    items = phrases.load_phrases(default_path=path, user_path=user)
    assert items[0]['category'] == 'tired'


def test_select_phrase_accepts_a_category_list(database, tmp_path):
    path = _library(tmp_path, [
        {'id': 'care_a', 'text': '记得喝点水。', 'category': 'care',
         'cooldown_seconds': 0, 'daily_limit': 0, 'time_window': 'any'},
        {'id': 'idle_a', 'text': '我在这儿呢。', 'category': 'idle',
         'cooldown_seconds': 0, 'daily_limit': 0, 'time_window': 'any'},
        {'id': 'happy_a', 'text': '看到你笑真好。', 'category': 'happy',
         'cooldown_seconds': 0, 'daily_limit': 0, 'time_window': 'any'},
    ])
    now = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
    with database.transaction() as connection:
        for _index in range(10):
            chosen = phrases.select_phrase(
                connection, category=['care', 'idle'], now=now,
                default_path=path, user_path=tmp_path / 'user.json')
            assert chosen['id'] in ('care_a', 'idle_a')


def test_select_phrase_with_an_unknown_category_selects_nothing(database, tmp_path):
    path = _library(tmp_path, [
        {'id': 'care_a', 'text': '记得喝点水。', 'category': 'care',
         'cooldown_seconds': 0, 'daily_limit': 0, 'time_window': 'any'},
    ])
    now = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
    with database.transaction() as connection:
        assert phrases.select_phrase(connection, category='cheer', now=now,
                                     default_path=path,
                                     user_path=tmp_path / 'user.json') is None


def test_select_phrase_accepts_an_explicit_window(database, tmp_path):
    path = _library(tmp_path, [
        {'id': 'w_morning', 'text': '早上的台词。', 'category': 'care',
         'cooldown_seconds': 0, 'daily_limit': 0, 'time_window': 'morning'},
        {'id': 'w_night', 'text': '夜里的台词。', 'category': 'care',
         'cooldown_seconds': 0, 'daily_limit': 0, 'time_window': 'night'},
    ])
    with database.transaction() as connection:
        chosen = phrases.select_phrase(connection, category='care', window='night',
                                       default_path=path,
                                       user_path=tmp_path / 'user.json')
    assert chosen['id'] == 'w_night'


def test_select_phrase_window_is_ignored_when_now_is_given(database, tmp_path):
    path = _library(tmp_path, [
        {'id': 'w_morning', 'text': '早上的台词。', 'category': 'care',
         'cooldown_seconds': 0, 'daily_limit': 0, 'time_window': 'morning'},
    ])
    now = datetime(2026, 11, 4, 20, 0, tzinfo=timezone.utc)
    with database.transaction() as connection:
        assert phrases.select_phrase(connection, category='care', now=now,
                                     window='morning', default_path=path,
                                     user_path=tmp_path / 'user.json') is None


def test_select_phrase_honours_allowed_windows(database, tmp_path):
    path = _library(tmp_path, [
        {'id': 'w_morning', 'text': '早上的台词。', 'category': 'care',
         'cooldown_seconds': 0, 'daily_limit': 0, 'time_window': 'morning'},
        {'id': 'w_night', 'text': '夜里的台词。', 'category': 'care',
         'cooldown_seconds': 0, 'daily_limit': 0, 'time_window': 'night'},
    ])
    with database.transaction() as connection:
        chosen = phrases.select_phrase(
            connection, category='care', allowed_windows=('night',),
            default_path=path, user_path=tmp_path / 'user.json')
    assert chosen['id'] == 'w_night'


def test_shipped_library_serves_every_required_category(database):
    """The real library must return a line for each required intent.

    ``night`` is a night-only intent by design, so it is drawn at 23:00; the
    other intents must be reachable in the middle of the day.
    """
    noon = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
    late = datetime(2026, 11, 4, 23, 0, tzinfo=timezone.utc)
    for category in REQUIRED_CATEGORIES:
        now = late if category == 'night' else noon
        with database.transaction() as connection:
            chosen = phrases.select_phrase(connection, category=category, now=now)
        assert chosen is not None, 'no selectable %s line at %s' % (
            category, now.strftime('%H:%M'))
        assert chosen['category'] == category

    with database.transaction() as connection:
        chosen = phrases.select_phrase(connection, category='night', now=late)
    assert chosen is not None
    assert chosen['time_window'] == 'night'


def test_repeated_selection_respects_cooldowns(database):
    """Two draws inside the shortest possible cooldown never repeat a phrase."""
    now = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
    with database.transaction() as connection:
        first = phrases.select_phrase(connection, category='happy', now=now)
        assert first is not None
        second = phrases.select_phrase(
            connection, category='happy', now=now + timedelta(minutes=1))
        assert second is not None
        assert second['id'] != first['id']
