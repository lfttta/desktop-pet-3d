"""Persona card, prompt budget, style and phrase preference contracts."""

from datetime import datetime, timezone
import json
import random
from types import SimpleNamespace

import pytest

from service import persona
from service.ai import prompt
from service.database import Database
from service.expression import phrases, style
from service.features import expression as expression_feature
from service.protocol import ProtocolError
from service.settings import SettingsStore


@pytest.fixture(autouse=True)
def _reset_persona():
    persona.reset()
    yield
    persona.reset()


def test_persona_card_loads_and_public_catalog_stays_private():
    card = persona.load_card('godette')
    assert card['id'] == 'godette'
    assert card['display_name'] == 'Godette'
    assert 0 < len(card['summary']) <= persona.MAX_SUMMARY_CHARS
    assert 0 < len(card['prompt']) <= persona.MAX_PROMPT_CHARS
    assert card['voice']
    assert card['boundaries']

    assembled = persona.build_prompt('godette')
    assert card['summary'] in assembled
    assert card['prompt'] in assembled
    assert card['voice'] in assembled

    assert persona.public_catalog() == [{'id': 'godette', 'display_name': 'Godette'}]
    assert persona.resolve('godette') == {
        'id': 'godette', 'display_name': 'Godette', 'prompt': ''}
    assert persona.resolve('missing') is None


def test_missing_card_falls_back_to_the_default_summary_with_a_warning(tmp_path):
    card = persona.load_card('godette', path=tmp_path / 'missing.json')
    assert card['summary'] == persona.DEFAULT_SUMMARY
    assert card['source'] == 'default'
    assert persona.warnings()
    assert 'godette' in persona.warnings()[0]


def test_broken_card_falls_back_to_the_default_summary_with_a_warning(tmp_path):
    broken = tmp_path / 'broken.json'
    broken.write_text('{not json', encoding='utf-8')
    card = persona.load_card('godette', path=broken)
    assert card['summary'] == persona.DEFAULT_SUMMARY
    assert persona.warnings()


def test_invalid_summary_length_is_rejected(tmp_path):
    source = persona.card_path('godette')
    document = json.loads(source.read_text(encoding='utf-8'))
    document['summary'] = '长' * (persona.MAX_SUMMARY_CHARS + 1)
    invalid = tmp_path / 'long.json'
    invalid.write_text(json.dumps(document, ensure_ascii=False), encoding='utf-8')

    card = persona.load_card('godette', path=invalid)
    assert card['summary'] == persona.DEFAULT_SUMMARY
    assert persona.warnings()


def test_prompt_budget_keeps_output_rules_and_boundaries(tmp_path, monkeypatch):
    store = SettingsStore(tmp_path)
    database = Database(tmp_path / 'pet.db')
    # The rule text now carries the whole action vocabulary, so a fixed 900 would
    # sit below the mandatory core and prove nothing except that the core is
    # truncated. Derive a budget that leaves room for the state line and the
    # rules but not for style plus memory, which is the drop order under test.
    mandatory = (len(persona.build_prompt('godette').strip())
                 + len(persona.build_boundaries('godette').strip())
                 + len(prompt.OUTPUT_RULES))
    state_line = len('当前状态：') + 240
    budget = mandatory + state_line + 40
    try:
        monkeypatch.setattr(prompt, 'MAX_SYSTEM_CHARS', budget)
        monkeypatch.setattr(prompt, 'search_memories',
                            lambda *args, **kwargs: ['记忆' * 300 for _ in range(5)])
        monkeypatch.setattr(prompt, '_state_summary', lambda *args: '状态' * 150)
        messages = prompt.build_messages(database, '', '你好', store.settings)
    finally:
        database.close()

    system = messages[0]['content']
    assert messages[0]['role'] == 'system'
    assert len(system) <= prompt.MAX_SYSTEM_CHARS
    assert prompt.OUTPUT_RULES in system
    assert persona.build_boundaries('godette') in system
    # Under pressure the style and memory sections are dropped, never the rules.
    assert '表达风格' not in system
    assert '可能相关的记忆' not in system
    assert '当前状态' in system
    measured = sum(len(item['content']) + len(item['role']) + 4 for item in messages)
    assert measured <= prompt.MAX_PROMPT_CHARS


def test_system_prompt_addresses_the_user_by_name(tmp_path):
    # `user_name` had a settings control and no reader: the persona never learned
    # the name. One line in the system prompt is what makes the field mean
    # something.
    store = SettingsStore(tmp_path)
    store.update({'user_name': '小明'})
    database = Database(tmp_path / 'pet.db')
    try:
        messages = prompt.build_messages(database, '', '你好', store.settings)
    finally:
        database.close()

    assert '称呼用户为「小明」' in messages[0]['content']


def test_default_user_name_adds_no_address_line(tmp_path):
    store = SettingsStore(tmp_path)
    database = Database(tmp_path / 'pet.db')
    try:
        messages = prompt.build_messages(database, '', '你好', store.settings)
    finally:
        database.close()

    assert store.settings['user_name'] == '你'
    assert '称呼用户为' not in messages[0]['content']


def test_prompt_keeps_memories_and_style_when_they_fit(tmp_path, monkeypatch):
    store = SettingsStore(tmp_path)
    database = Database(tmp_path / 'pet.db')
    try:
        monkeypatch.setattr(prompt, 'search_memories',
                            lambda *args, **kwargs: ['用户喜欢下雨天'])
        messages = prompt.build_messages(database, '', '你好', store.settings)
    finally:
        database.close()

    system = messages[0]['content']
    assert len(system) <= prompt.MAX_SYSTEM_CHARS
    assert prompt.OUTPUT_RULES in system
    assert persona.build_boundaries('godette') in system
    assert '表达风格' in system
    assert '可能相关的记忆' in system
    assert '用户喜欢下雨天' in system


@pytest.mark.parametrize('value, expected', [
    (0.0, '用词偏正式、完整'),
    (0.34, '自然口语'),
    (0.67, '很随意，像朋友聊天'),
    (1.0, '很随意，像朋友聊天'),
])
def test_style_buckets_produce_natural_language(value, expected):
    settings = {'expression': {'enabled': True, 'style': {'casualness': value}}}
    instruction = prompt.style_instruction(settings)
    assert expected in instruction
    assert '不得为了风格改变事实、省略关键信息或隐藏不确定性' in instruction
    assert len(instruction) <= prompt.STYLE_LIMIT


def test_style_instruction_is_empty_when_expression_is_disabled():
    settings = {'expression': {'enabled': False, 'style': {'warmth': 1.0}}}
    assert prompt.style_instruction(settings) == ''
    assert prompt.style_instruction({}) == ''


def test_style_instruction_keeps_the_caution_sentence_at_the_limit():
    settings = {'expression': {'enabled': True, 'style': {
        'casualness': 1.0, 'warmth': 1.0, 'directness': 1.0,
        'playfulness': 1.0, 'sentence_length': 1.0, 'emoji_frequency': 1.0,
    }}}
    instruction = prompt.style_instruction(settings)
    assert len(instruction) <= prompt.STYLE_LIMIT
    assert instruction.endswith(prompt.STYLE_CAUTION)


def test_polish_removes_or_keeps_at_most_one_emoji():
    assert style.polish('好的，今天下雨了😀😀！',
                        profile={'emoji_frequency': 0.0}) == '今天下雨了！'
    assert style.polish('今天很棒😀👍！',
                        profile={'emoji_frequency': 0.5}) == '今天很棒😀！'
    assert style.polish('今天很棒😀👍！',
                        profile={'emoji_frequency': 1.0}) == '今天很棒😀！'
    assert style.polish('先别急。慢慢来😀😀',
                        profile={'emoji_frequency': 0.1}) == '先别急。慢慢来'
    # Without a profile the legacy deletion-only behaviour is unchanged.
    assert style.polish('今天很棒😀👍') == '今天很棒😀👍'


def test_polish_accepts_the_nested_settings_profile():
    assert style.polish('好的，我在😀',
                        profile={'expression': {'style': {
                            'emoji_frequency': 0.0}}}) == '我在'


def test_phrase_disable_persists_under_the_data_directory(tmp_path):
    data_dir = tmp_path / 'data'
    items = {item['id']: item for item in phrases.list_phrases(data_dir=data_dir)}
    assert items['idle_presence']['enabled'] is True
    assert items['idle_presence']['source'] == 'default'

    updated = phrases.set_enabled('idle_presence', False, data_dir=data_dir)
    assert updated['enabled'] is False

    local = data_dir / 'expression' / 'phrases.local.json'
    assert local.is_file()
    saved = json.loads(local.read_text(encoding='utf-8'))
    assert any(entry.get('id') == 'idle_presence' and entry.get('enabled') is False
               for entry in saved['phrases'])

    reloaded = {item['id']: item for item in phrases.list_phrases(data_dir=data_dir)}
    assert reloaded['idle_presence']['enabled'] is False
    assert reloaded['idle_presence']['source'] == 'local'

    phrases.set_enabled('idle_presence', True, data_dir=data_dir)
    restored = {item['id']: item for item in phrases.list_phrases(data_dir=data_dir)}
    assert restored['idle_presence']['enabled'] is True
    assert restored['idle_presence']['source'] == 'local'
    assert not list((data_dir / 'expression').glob('*.tmp'))

    with pytest.raises(KeyError):
        phrases.set_enabled('missing-phrase', False, data_dir=data_dir)


def test_select_phrase_reads_the_data_directory_override(tmp_path):
    database = Database(tmp_path / 'pet.db')
    try:
        phrases.set_enabled('idle_presence', False, data_dir=tmp_path)
        with database.transaction() as connection:
            chosen = phrases.select_phrase(
                connection, category='idle',
                now=datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc),
                rng=random.Random(0))
        assert chosen is not None
        assert chosen['id'] != 'idle_presence'
    finally:
        database.close()


def test_expression_commands_list_and_toggle(tmp_path):
    store = SettingsStore(tmp_path)
    context = SimpleNamespace()

    event, payload = expression_feature.list_phrases(
        {'id': 'list', 'payload': {}}, store, context)
    assert event == 'result'
    assert payload['count'] == len(payload['phrases'])
    assert all(set(item) == set(expression_feature.PHRASE_FIELDS)
               for item in payload['phrases'])

    event, payload = expression_feature.set_enabled(
        {'id': 'toggle', 'payload': {'phrase_id': 'idle_presence',
                                     'enabled': False}}, store, context)
    assert event == 'result'
    assert payload == {'saved': True, 'phrase_id': 'idle_presence',
                       'enabled': False}
    assert (tmp_path / 'expression' / 'phrases.local.json').is_file()

    assert expression_feature.CAPABILITY['available'] is True
    assert expression_feature.CAPABILITY['commands'] == [
        'expression.phrases.list', 'expression.phrases.set_enabled']


def test_expression_set_enabled_rejects_invalid_input(tmp_path):
    store = SettingsStore(tmp_path)
    context = SimpleNamespace()
    for payload in ({'phrase_id': 'idle_presence'},
                    {'phrase_id': '', 'enabled': True},
                    {'phrase_id': 'missing-phrase', 'enabled': True},
                    {'phrase_id': 'idle_presence', 'enabled': 'yes'}):
        with pytest.raises(ProtocolError) as caught:
            expression_feature.set_enabled(
                {'id': 'bad', 'payload': payload}, store, context)
        assert caught.value.code == 'invalid_request'
        assert caught.value.request_id == 'bad'
