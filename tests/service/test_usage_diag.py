"""Usage panel aggregation and redacted diagnostic bundle contracts."""

import base64
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import zipfile

import pytest

from service.ai.budget import BudgetLedger
from service.commands import dispatch
from service.database import SCHEMA_VERSION, Database
from service.features import diag
from service.protocol import ProtocolError
from service.runtime import EventLog
from service.settings import SettingsStore

REPORT_KEYS = {'today', 'month', 'limits', 'ratio', 'status', 'by_model',
               'by_day', 'price_configured'}
TOTALS_KEYS = {'tokens', 'cost', 'calls'}


class _Context:
    def __init__(self, database):
        self.database = database
        self.log = EventLog()
        self.frames = []

    def emit(self, event, payload):
        self.frames.append((event, payload))

    def post(self, event, payload):
        self.frames.append((event, payload))

    def register_tick(self, hook):
        pass


class _Environment:
    def __init__(self, tmp_path):
        self.data_dir = Path(tmp_path)
        self.store = SettingsStore(self.data_dir)
        self.database = Database(self.data_dir / 'pet.db')
        self.context = _Context(self.database)

    def dispatch(self, command, payload=None):
        return dispatch({'version': 1, 'id': 'req-usage-diag', 'type': command,
                         'payload': payload or {}}, self.store, self.context)

    def close(self):
        self.database.close()


@pytest.fixture
def env(tmp_path):
    environment = _Environment(tmp_path)
    yield environment
    environment.close()


def _now_stamp(days_ago=0, now=None):
    moment = (now or datetime.now(timezone.utc)) - timedelta(days=days_ago)
    return moment.strftime('%Y-%m-%d %H:%M:%S')


def _previous_month_stamp():
    first_of_month = datetime.now(timezone.utc).date().replace(day=1)
    return (first_of_month - timedelta(days=1)).strftime('%Y-%m-%d %H:%M:%S')


def _insert_usage(env, model, prompt, completion, cost, created_at=None):
    with env.database.transaction() as connection:
        connection.execute(
            'INSERT INTO usage_ledger(model, prompt_tokens, completion_tokens, '
            'total_tokens, cost, created_at) VALUES (?,?,?,?,?,?)',
            (model, prompt, completion, prompt + completion, cost,
             created_at or _now_stamp()))


def _seed_private_content(env):
    with env.database.transaction() as connection:
        connection.execute(
            "INSERT INTO conversations(id, title) VALUES ('c-secret', 'SECRET_TITLE')")
        connection.execute(
            "INSERT INTO messages(id, conversation_id, role, content) "
            "VALUES ('m-secret', 'c-secret', 'user', 'SECRET_MESSAGE_BODY')")
        connection.execute(
            "INSERT INTO memories(id, content) VALUES ('mem-secret', 'SECRET_MEMORY_BODY')")


def test_usage_report_today_month_totals_and_shape(env):
    env.store.update({'ai': {'daily_token_budget': 1000,
                             'monthly_cost_budget': 1.0,
                             'currency': 'USD',
                             'input_price_per_million': 0.0,
                             'output_price_per_million': 0.0}})
    _insert_usage(env, 'model-a', 100, 50, 0.002, _now_stamp())
    _insert_usage(env, 'model-b', 200, 100, 0.5, _previous_month_stamp())

    event, payload = env.dispatch('usage.get')

    assert event == 'result'
    assert set(payload) == REPORT_KEYS
    assert set(payload['today']) == TOTALS_KEYS
    assert payload['today'] == {'tokens': 150, 'cost': 0.002, 'calls': 1}
    assert payload['month'] == {'tokens': 150, 'cost': 0.002, 'calls': 1}
    assert payload['limits'] == {'daily_tokens': 1000, 'monthly_cost': 1.0,
                                 'currency': 'USD'}
    assert payload['ratio'] == {'tokens': 0.15, 'cost': 0.002}
    assert payload['status'] == 'ok'
    assert payload['price_configured'] is False
    assert payload['by_model'] == [{'model': 'model-a', 'tokens': 150,
                                    'cost': 0.002, 'calls': 1}]
    assert len(payload['by_day']) == 14
    assert payload['by_day'][-1]['date'] == \
        datetime.now(timezone.utc).date().isoformat()
    assert payload['by_day'][-1]['tokens'] == 150
    assert payload['by_day'][-1]['calls'] == 1
    assert all(set(day) == {'date', 'tokens', 'cost', 'calls'}
               for day in payload['by_day'])


def test_usage_report_zero_price_means_price_not_configured(env):
    _insert_usage(env, 'model-a', 1000, 1000, 0.0, _now_stamp())

    _, payload = env.dispatch('usage.get')
    assert payload['price_configured'] is False
    assert payload['month']['cost'] == 0.0

    env.store.update({'ai': {'input_price_per_million': 0.5}})
    _, payload = env.dispatch('usage.get')
    assert payload['price_configured'] is True


@pytest.mark.parametrize('daily_budget,expected', [
    (200, 'ok'),
    (180, 'warning'),
    (100, 'hard_limit'),
])
def test_usage_status_reuses_budget_policy(env, daily_budget, expected):
    _insert_usage(env, 'model-a', 100, 50, 0.0, _now_stamp())

    env.store.update({'ai': {'daily_token_budget': daily_budget,
                             'monthly_cost_budget': 1.0}})
    _, payload = env.dispatch('usage.get')

    assert payload['status'] == expected
    assert payload['ratio']['tokens'] == round(150 / daily_budget, 4)


def test_usage_report_without_database_reports_zeroes(tmp_path):
    store = SettingsStore(tmp_path)
    _, payload = dispatch({'version': 1, 'id': 'no-db', 'type': 'usage.get',
                           'payload': {}}, store)
    assert payload['today'] == {'tokens': 0, 'cost': 0.0, 'calls': 0}
    assert payload['month'] == {'tokens': 0, 'cost': 0.0, 'calls': 0}
    assert payload['by_model'] == []
    assert len(payload['by_day']) == 14
    assert payload['status'] == 'ok'


def test_budget_ledger_aggregates_by_model_and_day(env):
    now = datetime.now(timezone.utc)
    today_stamp = _now_stamp(now=now)
    yesterday_stamp = _now_stamp(days_ago=1, now=now)
    oldest_stamp = _now_stamp(days_ago=13, now=now)
    _insert_usage(env, 'model-a', 10, 5, 0.01, today_stamp)
    _insert_usage(env, 'model-a', 20, 5, 0.02, yesterday_stamp)
    _insert_usage(env, 'model-d', 3, 2, 0.001, today_stamp)
    # These rows exercise the 14-day window edge and the month filter. Whether
    # they land in the current month depends on the calendar date, so the
    # by_model expectation is derived below instead of hard-coded.
    _insert_usage(env, 'model-b', 100, 100, 0.5, oldest_stamp)
    _insert_usage(env, 'model-c', 1000, 1000, 9.0,
                  _now_stamp(days_ago=40, now=now))

    ledger = BudgetLedger(env.database)
    current_month = now.strftime('%Y-%m')

    def in_month(value):
        return value[:7] == current_month

    expected_models = ['model-a', 'model-d']
    if in_month(oldest_stamp):
        expected_models.insert(0, 'model-b')
    by_model = ledger.by_model()
    assert [entry['model'] for entry in by_model] == expected_models

    entries = {entry['model']: entry for entry in by_model}
    # Yesterday falls into the previous month only on the first day of a month.
    assert entries['model-a']['tokens'] == (40 if in_month(yesterday_stamp) else 15)
    assert entries['model-a']['calls'] == (2 if in_month(yesterday_stamp) else 1)
    assert entries['model-d']['tokens'] == 5
    assert entries['model-d']['calls'] == 1
    assert 'model-c' not in entries

    days = ledger.by_day(14)
    assert len(days) == 14
    assert days[-1]['tokens'] == 20
    assert days[-2]['tokens'] == 25
    assert days[0]['tokens'] == 200
    assert sum(day['tokens'] for day in days) == 245


def test_diag_export_contains_redacted_files_and_no_private_bodies(env, tmp_path):
    _seed_private_content(env)
    env.store.update({'ai': {'provider': 'deepseek',
                             'input_price_per_million': 1.0}})
    env.context.log.record('info', 'diag', 'export_started',
                           'call with sk-ABCDEFGH12345678')
    env.context.log.record('error', 'ai', 'provider_failed',
                           'Authorization: Bearer abcdefgh12345678')

    event, payload = env.dispatch('diag.export')

    assert event == 'result'
    assert payload['included'] == ['manifest.json', 'settings.json',
                                   'diagnostics.json', 'logs.txt']
    target = Path(payload['path'])
    assert target.is_file()
    assert target.resolve().parent == (tmp_path / 'exports').resolve()
    assert (tmp_path / 'exports') in target.resolve().parents
    assert payload['bytes'] == target.stat().st_size
    assert payload['bytes'] <= diag.MAX_BUNDLE_BYTES

    with zipfile.ZipFile(target) as archive:
        assert archive.namelist() == payload['included']
        contents = {name: archive.read(name) for name in archive.namelist()}

    for name in contents:
        assert '/' not in name and '\\' not in name
        assert Path(name).is_absolute() is False

    manifest = json.loads(contents['manifest.json'])
    assert manifest['format_version'] == diag.BUNDLE_FORMAT_VERSION
    assert [item['name'] for item in manifest['files']] == payload['included'][1:]
    for item in manifest['files']:
        assert item['bytes'] == len(contents[item['name']])

    settings = json.loads(contents['settings.json'])
    assert settings['ai']['provider'] == 'deepseek'
    assert 'api_key' not in json.dumps(settings)
    assert 'sk-' not in contents['settings.json'].decode('utf-8')

    diagnostics = json.loads(contents['diagnostics.json'])
    assert diagnostics['counts']['messages'] == 1
    assert diagnostics['counts']['memories'] == 1
    assert diagnostics['counts']['conversations'] == 1
    assert diagnostics['database_user_version'] == SCHEMA_VERSION
    assert diagnostics['event_log_entries'] == 2

    logs = contents['logs.txt'].decode('utf-8')
    assert 'export_started' in logs
    assert 'provider_failed' in logs
    assert 'sk-ABCDEFGH12345678' not in logs
    assert 'abcdefgh12345678' not in logs
    assert '[redacted]' in logs

    for data in contents.values():
        assert b'SECRET_TITLE' not in data
        assert b'SECRET_MESSAGE_BODY' not in data
        assert b'SECRET_MEMORY_BODY' not in data
        assert b'sk-ABCDEFGH12345678' not in data


def test_diag_logs_use_the_bounded_tail_of_two_hundred_entries(env, tmp_path):
    for index in range(205):
        env.context.log.record('info', 'diag', f'evt_{index:03d}', 'event')

    _, payload = env.dispatch('diag.export')

    with zipfile.ZipFile(payload['path']) as archive:
        logs = archive.read('logs.txt').decode('utf-8')
    assert 'evt_004' not in logs
    assert 'evt_005' in logs
    assert 'evt_204' in logs
    assert len(logs.splitlines()) == diag.LOG_TAIL


def test_diag_bundle_stays_within_the_two_megabyte_cap(env, monkeypatch):
    oversized = base64.b64encode(os.urandom(3 * 1024 * 1024)).decode('ascii')
    monkeypatch.setattr(diag, '_log_text', lambda ctx: oversized)

    _, payload = env.dispatch('diag.export')

    assert payload['bytes'] <= diag.MAX_BUNDLE_BYTES
    with zipfile.ZipFile(payload['path']) as archive:
        assert archive.namelist() == payload['included']
        assert archive.read('logs.txt') == b''


def test_export_path_is_confined_to_the_data_directory(env, tmp_path):
    assert diag._inside_data_dir(tmp_path, tmp_path / 'exports' / 'diag-x.zip')
    assert diag._inside_data_dir(tmp_path, tmp_path)
    assert not diag._inside_data_dir(tmp_path, tmp_path / '..' / 'diag-x.zip')
    assert not diag._inside_data_dir(tmp_path,
                                     tmp_path.parent / 'elsewhere' / 'diag-x.zip')

    _, payload = env.dispatch('diag.export')
    assert diag._inside_data_dir(tmp_path, payload['path'])


def test_export_fails_cleanly_without_a_data_directory(env):
    class _StoreWithoutPath:
        settings = {}
        warnings = []

    with pytest.raises(ProtocolError) as caught:
        diag.export({'id': 'no-dir', 'payload': {}}, _StoreWithoutPath(), env.context)
    assert caught.value.code == 'storage_unavailable'
    assert caught.value.request_id == 'no-dir'
