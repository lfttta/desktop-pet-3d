"""Prompt context contracts: traits, tone, summary and memory budget."""

import json

import pytest

from service.ai import prompt
from service.database import Database
from service.memory.store import MemoryStore
from service.persona_tone import TONE_DIRECTIVES
from service.settings import SettingsStore


@pytest.fixture
def store(tmp_path):
    return SettingsStore(tmp_path)


@pytest.fixture
def database(tmp_path):
    instance = Database(tmp_path / 'pet.db')
    yield instance
    instance.close()


def _insert_state(database, traits):
    payload = {
        'pad': {'pleasure': 0.3, 'arousal': -0.1, 'dominance': 0.0},
        'psych': {'energy': 0.6, 'affinity': 0.45, 'stress': 0.1},
        'personality': {'traits': traits},
        'updated_at': '2026-11-04T00:00:00+00:00',
    }
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO character_state(subject_user_id, payload) VALUES('owner', ?)",
            (json.dumps(payload),))


def test_personality_traits_join_the_state_summary(database, store):
    _insert_state(database, {
        'kindness': 0.78,
        'independence': 0.62,
        'warmth': 0.62,
        'curiosity': 0.5,
        'playfulness': 0.5,
        'directness': 0.5,
    })

    messages = prompt.build_messages(database, '', '你好', store.settings)
    system = messages[0]['content']
    state = system.split('当前状态：', 1)[1].split('\n', 1)[0]

    assert state.startswith('traits=warmth:0.62,curiosity:0.5')
    assert 'playfulness:0.5' in state
    assert 'pleasure=0.30' in state
    assert 'affinity=0.45' in state
    # Traits, pad and psych share one six-item cap; pad and psych come first.
    assert len(state.split('，')) == prompt.MAX_STATE_ITEMS
    assert 'stress' not in state
    # Compact text, never a JSON blob.
    assert '{' not in state and '"' not in state
    # Fixed core card traits are not "approved" growth and stay out.
    assert 'kindness' not in state


def test_state_summary_without_personality_keeps_pad_and_psych(database, store):
    payload = {
        'pad': {'pleasure': 0.1, 'arousal': 0.0, 'dominance': 0.0},
        'psych': {'energy': 0.6, 'affinity': 0.45, 'stress': 0.1},
    }
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO character_state(subject_user_id, payload) VALUES('owner', ?)",
            (json.dumps(payload),))

    messages = prompt.build_messages(database, '', '你好', store.settings)
    state = messages[0]['content'].split('当前状态：', 1)[1].split('\n', 1)[0]
    assert 'traits=' not in state
    assert 'pleasure=0.10' in state
    assert 'stress=0.10' in state


@pytest.mark.parametrize('tone', ['godette', 'sharp', 'clingy', 'tool'])
def test_each_tone_adds_its_own_directive(tone):
    settings = {'expression': {'tone': tone}}
    messages = prompt.build_messages(None, '', '你好', settings)
    system = messages[0]['content']

    directive = TONE_DIRECTIVES[tone]
    assert directive
    assert directive in system
    for other in ('godette', 'sharp', 'clingy', 'tool'):
        if other != tone:
            assert TONE_DIRECTIVES[other] not in system


def test_custom_tone_adds_nothing_and_missing_tone_defaults_to_godette():
    custom = prompt.build_messages(None, '', '你好',
                                   {'expression': {'tone': 'custom'}})
    assert TONE_DIRECTIVES['custom'] == ''
    for directive in TONE_DIRECTIVES.values():
        assert not directive or directive not in custom[0]['content']

    default = prompt.build_messages(None, '', '你好', {})
    assert TONE_DIRECTIVES['godette'] in default[0]['content']


def test_sharp_and_clingy_keep_their_limits():
    assert '毒舌' in TONE_DIRECTIVES['sharp']
    assert '人身攻击' in TONE_DIRECTIVES['sharp']
    assert '黏人' in TONE_DIRECTIVES['clingy']
    assert '道德绑架' in TONE_DIRECTIVES['clingy']
    assert '简洁高效' in TONE_DIRECTIVES['tool']


def test_earlier_summary_is_injected(database, store, monkeypatch):
    monkeypatch.setattr(prompt, 'load_summary',
                        lambda database, conversation_id: '用户上周在准备考试。')
    messages = prompt.build_messages(database, 'c-1', '你好', store.settings)
    assert '更早的对话摘要：用户上周在准备考试。' in messages[0]['content']


def test_earlier_summary_is_capped_at_800_chars(database, store, monkeypatch):
    monkeypatch.setattr(prompt, 'load_summary',
                        lambda database, conversation_id: '摘' * 1200)
    messages = prompt.build_messages(database, 'c-1', '你好', store.settings)
    system = messages[0]['content']
    summary = system.split('更早的对话摘要：', 1)[1].split('\n', 1)[0]
    assert len(summary) == prompt.MAX_SUMMARY_CHARS == 800


def test_missing_summary_module_is_skipped(database, store, monkeypatch):
    monkeypatch.setattr(prompt, 'load_summary', None)
    messages = prompt.build_messages(database, 'c-1', '你好', store.settings)
    assert '更早的对话摘要' not in messages[0]['content']


def test_memory_uses_the_context_search_and_forwards_limits(database, store, monkeypatch):
    from service.memory import search as memory_search

    calls = []

    def fake(database_arg, query, settings, limit, total_chars=None,
             entry_chars=None, with_ids=False):
        calls.append((database_arg, query, settings, limit, total_chars,
                      entry_chars, with_ids))
        hits = ['甲' * 50, {'text': '乙' * 50}, '丙' * 50]
        if with_ids:
            return hits, ['m-a', 'm-b', 'm-c']
        return hits

    monkeypatch.setattr(memory_search, 'search_for_context', fake, raising=False)
    store.update({'memory': {'max_injected': 2, 'max_entry_chars': 10}})

    messages = prompt.build_messages(database, '', '你好下雨天', store.settings,
                                     store)
    system = messages[0]['content']

    assert len(calls) == 1
    database_arg, query, settings_arg, limit, total_chars, entry_chars, with_ids = calls[0]
    assert database_arg is database
    assert query == '你好下雨天'
    # The SettingsStore (not the plain dict) is forwarded so embedding
    # retrieval can reach the transport and key provider.
    assert settings_arg is store
    assert settings_arg.settings['memory']['max_injected'] == 2
    assert (limit, total_chars, entry_chars, with_ids) == (2, 1600, 10, True)
    # Two entries reach the prompt, each capped by max_entry_chars.
    assert '甲' * 10 in system
    assert '乙' * 10 in system
    assert '丙' not in system


def test_memory_falls_back_to_search_memories(database, store, monkeypatch):
    from service.memory import search as memory_search

    def missing(*args, **kwargs):
        raise AttributeError('search_for_context')

    monkeypatch.setattr(memory_search, 'search_for_context', missing, raising=False)
    monkeypatch.setattr(prompt, 'search_memories',
                        lambda *args, **kwargs: ['旧接口的回忆'])

    messages = prompt.build_messages(database, '', '你好', store.settings)
    assert '旧接口的回忆' in messages[0]['content']


def test_memory_budget_is_1600_chars(database, store, monkeypatch):
    monkeypatch.setattr(prompt, '_context_memory_hits',
                        lambda *args, **kwargs: (['记' * 500 for _ in range(5)], []))

    messages = prompt.build_messages(database, '', '你好', store.settings)
    system = messages[0]['content']
    body = system.split('可能相关的记忆：\n', 1)[1]
    body = body[:body.index(prompt.OUTPUT_RULES)].rstrip('\n')

    assert prompt.MAX_MEMORY_CHARS == 1600
    assert len(body) <= 1600
    # Five full entries are selectable; the joined body must fit the budget.
    assert body.count('- ') >= 3


def test_system_block_stays_within_the_total_prompt_budget(database, store):
    messages = prompt.build_messages(database, '', '你好', store.settings)
    assert len(messages[0]['content']) <= prompt.MAX_SYSTEM_CHARS
    assert prompt.MAX_SYSTEM_CHARS <= prompt.MAX_PROMPT_CHARS


def test_three_long_memories_all_fit_the_prompt(database, store):
    MemoryStore(database)
    contents = ['下雨天' * 160 + str(index) for index in range(3)]
    with database.transaction() as connection:
        for index, content in enumerate(contents):
            connection.execute(
                'INSERT INTO memories(id, content, importance) VALUES(?,?,?)',
                ('m-long-%d' % index, content, 0.5))

    messages = prompt.build_messages(database, '', '下雨天', store.settings)
    system = messages[0]['content']

    # The 500-char value is a per-entry cap; three 481-char memories still fit
    # inside the separate 1600-char total memory budget.
    for content in contents:
        assert content in system
    body = system.split('可能相关的记忆：\n', 1)[1]
    body = body[:body.index(prompt.OUTPUT_RULES)].rstrip('\n')
    assert len(body) <= prompt.MAX_MEMORY_CHARS


def test_prompt_counts_only_the_memory_that_reaches_it(database, store):
    MemoryStore(database)
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO memories(id, content, importance) VALUES('m-a','下雨天甲',0.5)")
        connection.execute(
            "INSERT INTO memories(id, content, importance) VALUES('m-b','下雨天乙',0.5)")
    store.update({'memory': {'max_injected': 1}})

    prompt.build_messages(database, '', '下雨天', store.settings)

    counts = [row[0] for row in database.connection.execute(
        'SELECT recall_count FROM memories ORDER BY id')]
    assert sorted(counts) == [0, 1]


def test_history_keeps_twenty_four_rounds(database, store):
    with database.transaction() as connection:
        connection.execute("INSERT INTO conversations(id) VALUES('c-24')")
        for index in range(30):
            connection.execute(
                'INSERT INTO messages(id, conversation_id, role, content) '
                'VALUES(?,?,?,?)',
                ('m-%02d' % index, 'c-24',
                 'user' if index % 2 == 0 else 'assistant',
                 '消息%02d' % index))

    messages = prompt.build_messages(database, 'c-24', '你好', store.settings)
    history = [item for item in messages[1:-1]]
    assert prompt.MAX_HISTORY_MESSAGES == 24
    assert prompt.MAX_PROMPT_CHARS == 6000
    assert len(history) == 24
    assert history[0]['content'] == '消息06'
    assert history[-1]['content'] == '消息29'
