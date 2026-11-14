"""Prompt budget trimming and memory injection edge cases.

``build_messages`` has to hold a hard character budget while keeping the output
rules and boundaries intact. Trimming happens in a fixed order, and every step
is reachable only when the prompt is genuinely too long; these tests drive each
step with oversized inputs instead of trusting the order by inspection.
"""

from copy import deepcopy
import json
import sqlite3

import pytest

from service.ai import prompt as prompt_module
from service.database import Database
from service.settings import DEFAULTS


@pytest.fixture()
def database(tmp_path):
    db = Database(tmp_path / 'pet.db')
    yield db
    db.close()


def _settings():
    return deepcopy(DEFAULTS)


def _long_text(length):
    return '字' * length


# -- system block trimming -------------------------------------------------


def test_a_short_prompt_keeps_every_optional_section():
    settings = _settings()
    settings['user_name'] = '小明'

    messages = prompt_module.build_messages(
        None, 'c1', '你好', settings)
    system = messages[0]['content']

    assert '表达风格：' in system
    assert '称呼用户' in system
    assert prompt_module.OUTPUT_RULES[:20] in system


def test_fit_system_drops_memory_before_the_summary():
    budget = prompt_module.MAX_SYSTEM_CHARS
    core = 'c' * (budget // 2)
    summary_line = 's' * (budget // 4)
    memory_line = 'm' * (budget // 2)

    parts = prompt_module._fit_system(
        core, 'B', '', '', '', summary_line, memory_line, '')

    assert summary_line in parts, 'the summary outlives raw memory hits'
    assert memory_line not in parts


def _room_for_core():
    """Characters the core paragraph may take before optional lines must go."""
    fixed = len(prompt_module.OUTPUT_RULES) + 1  # the newline before it
    return prompt_module.MAX_SYSTEM_CHARS - fixed - 2


def test_fit_system_drops_the_address_line_before_the_style_line():
    style_line = 'y' * 200
    address_line = 'a' * 400
    # Fill the rest of the block so the address line cannot fit next to style.
    core = 'c' * (_room_for_core() - len(style_line) - 1)

    parts = prompt_module._fit_system(
        core, 'B', style_line, '', '', '', '', address_line)

    assert style_line in parts, 'style outlives the address line'
    assert address_line not in parts


def test_fit_system_shortens_the_state_before_the_core_paragraph():
    state_line = 't' * 400
    core = 'c' * (_room_for_core() - 10)

    parts = prompt_module._fit_system(
        core, 'B', '', '', state_line, '', '', '')

    joined = '\n'.join(parts)
    assert len(joined) <= prompt_module.MAX_SYSTEM_CHARS
    assert core in parts, 'the persona paragraph is the last thing to give way'
    assert state_line not in parts, 'the state line is shortened first'


def test_fit_system_never_drops_the_boundaries_or_output_rules():
    budget = prompt_module.MAX_SYSTEM_CHARS
    core = 'c' * (budget * 2)

    parts = prompt_module._fit_system(
        core, 'BOUNDARIES', 'y' * 900, 'tone', 't' * 900, 's' * 900,
        'm' * 900, 'a' * 900)

    assert 'BOUNDARIES' in parts
    assert prompt_module.OUTPUT_RULES in parts


def test_the_real_caps_leave_the_trimming_steps_unreached():
    """The shipped caps never fill the system block, so the late drop steps are
    inert with today's persona. If a cap grows, this test fails and the trim
    order needs a fresh look rather than silently changing behaviour."""
    settings = _settings()
    settings['user_name'] = '字' * prompt_module.MAX_ADDRESS_CHARS
    biggest_memory = prompt_module._memory_line(
        ['字' * 400 for _ in range(10)])[0]
    biggest_summary = '字' * prompt_module.MAX_SUMMARY_CHARS

    system, _injected = prompt_module._system_message(
        settings, '字' * 240, ['字' * 400 for _ in range(10)], biggest_summary)

    assert prompt_module.MAX_MEMORY_CHARS >= len(biggest_memory) - 8
    assert '可能相关的记忆：' in system
    assert '更早的对话摘要：' in system
    assert '称呼用户' in system
    assert '表达风格：' in system
    assert len(system) <= prompt_module.MAX_SYSTEM_CHARS


def test_an_oversized_summary_is_dropped_before_the_address_line():
    settings = _settings()
    settings['user_name'] = '小明'

    system, _injected = prompt_module._system_message(
        settings, '', [], _long_text(6000))

    assert '称呼用户' in system, 'the address line outlives the older summary'
    assert _long_text(6000) not in system


def test_the_boundaries_and_output_rules_survive_any_trimming():
    settings = _settings()

    system, _injected = prompt_module._system_message(
        settings, _long_text(20000), [_long_text(900) for _ in range(10)],
        _long_text(900))

    assert '安全边界' in system
    assert prompt_module.OUTPUT_RULES[:20] in system
    assert len(system) <= prompt_module.MAX_SYSTEM_CHARS


def test_a_state_line_is_shortened_rather_than_breaking_the_budget():
    settings = _settings()

    system, _injected = prompt_module._system_message(
        settings, _long_text(4000), [], '')

    assert len(system) <= prompt_module.MAX_SYSTEM_CHARS


# -- memory allowance ------------------------------------------------------


def test_memory_lines_stop_at_the_total_allowance():
    memories = [_long_text(500) for _ in range(10)]

    block, indexes = prompt_module._memory_line(memories)

    assert len(block) <= prompt_module.MAX_MEMORY_CHARS + len('可能相关的记忆：\n')
    assert indexes == [0, 1, 2], 'only the hits that fit are reported as recalled'


def test_a_non_string_memory_hit_is_skipped_without_shifting_the_indexes():
    memories = [None, '', 42, '可用的一条']

    block, indexes = prompt_module._memory_line(memories)

    assert '可用的一条' in block
    assert indexes == [3], 'the index must point at the hit, not at its position in the kept list'


def test_no_usable_memory_returns_an_empty_block():
    assert prompt_module._memory_line([None, '', 7]) == ('', [])
    assert prompt_module._memory_line([]) == ('', [])


# -- history and total budget ----------------------------------------------


def _seed(database, conversation_id, count, length):
    with database.transaction() as connection:
        connection.execute(
            "INSERT OR IGNORE INTO conversations (id, title) VALUES (?, '')",
            (conversation_id,))
        for index in range(count):
            connection.execute(
                'INSERT INTO messages (conversation_id, role, content, created_at) '
                "VALUES (?, 'user', ?, ?)",
                (conversation_id, ('%03d' % index) + '字' * length,
                 '2026-01-01T00:%02d:%02dZ' % (index // 60, index % 60)))


def test_history_is_trimmed_to_the_message_cap(database):
    _seed(database, 'c1', 40, 10)

    messages = prompt_module.build_messages(
        database, 'c1', '继续', _settings())
    history = messages[1:-1]

    assert len(history) <= prompt_module.MAX_HISTORY_MESSAGES
    assert history[-1]['content'].startswith('039'), 'the newest turns are kept'


def test_the_whole_prompt_stays_inside_the_budget(database):
    _seed(database, 'c1', 60, 300)

    messages = prompt_module.build_messages(
        database, 'c1', _long_text(9000), _settings())

    total = sum(len(item['content']) for item in messages)
    assert total <= prompt_module.MAX_PROMPT_CHARS
    assert messages[0]['role'] == 'system'
    assert messages[-1]['role'] == 'user'


def test_an_over_long_user_turn_is_shortened_from_the_front(database):
    messages = prompt_module.build_messages(
        None, 'c1', _long_text(20000), _settings())

    assert len(messages[-1]['content']) < 20000
    assert messages[-1]['content'].endswith('字')


def test_history_older_than_the_budget_is_dropped_first(database):
    _seed(database, 'c1', 30, 400)

    messages = prompt_module.build_messages(
        database, 'c1', '你好', _settings())

    assert len(messages) >= 2
    assert messages[-1]['content'] == '你好', 'the current turn survives trimming'


# -- summary loading -------------------------------------------------------


def test_a_missing_summary_module_yields_no_summary(monkeypatch):
    monkeypatch.setattr(prompt_module, 'load_summary', None)

    assert prompt_module._conversation_summary(object(), 'c1') == ''


def test_a_dict_summary_is_read_from_its_text_field(monkeypatch):
    monkeypatch.setattr(prompt_module, 'load_summary',
                        lambda database, cid: {'summary': '  之前聊过猫  '})

    assert prompt_module._conversation_summary(object(), 'c1') == '之前聊过猫'


def test_a_loader_that_raises_is_ignored(monkeypatch):
    def explode(*_args):
        raise RuntimeError('boom')

    monkeypatch.setattr(prompt_module, 'load_summary', explode)

    assert prompt_module._conversation_summary(object(), 'c1') == ''


def test_a_summary_longer_than_the_cap_is_truncated(monkeypatch):
    monkeypatch.setattr(prompt_module, 'load_summary',
                        lambda database, cid: _long_text(5000))

    value = prompt_module._conversation_summary(object(), 'c1')

    assert len(value) == prompt_module.MAX_SUMMARY_CHARS


# -- state summary ---------------------------------------------------------


def test_the_state_summary_includes_approved_traits(database):
    with database.transaction() as connection:
        connection.execute(
            'INSERT OR REPLACE INTO character_state (subject_user_id, payload, updated_at) '
            "VALUES ('owner', ?, '2026-01-01T00:00:00Z')",
            (json.dumps({'personality': {'warmth': 0.62, 'curiosity': 0.4},
                         'pad': {'pleasure': 0.5}}),))

    summary = prompt_module._state_summary(database)

    assert 'traits=warmth:0.62' in summary
    assert 'pleasure=0.50' in summary


def test_the_state_summary_caps_its_items(database):
    with database.transaction() as connection:
        connection.execute(
            'INSERT OR REPLACE INTO character_state (subject_user_id, payload, updated_at) '
            "VALUES ('owner', ?, '2026-01-01T00:00:00Z')",
            (json.dumps({'personality': {'warmth': 0.6},
                         'pad': {('k%d' % i): 0.1 for i in range(10)},
                         'psych': {('p%d' % i): 0.2 for i in range(10)}}),))

    summary = prompt_module._state_summary(database)

    assert len(summary.split('，')) <= prompt_module.MAX_STATE_ITEMS


def test_an_unreadable_state_row_yields_no_summary():
    class _Broken:
        class connection:
            @staticmethod
            def execute(*_args):
                raise sqlite3.Error('no table')

    assert prompt_module._state_summary(_Broken()) == ''


def test_no_database_yields_no_summary():
    assert prompt_module._state_summary(None) == ''


# -- parameter defence -----------------------------------------------------


@pytest.mark.parametrize('limit,max_chars', [
    (None, 500), ('many', 500), (0, 500), (-3, 500),
    (5, None), (5, 'wide'), (5, 0),
])
def test_context_memory_hits_survive_unusable_limits(database, limit, max_chars):
    """A garbage limit falls back to the default instead of raising: the value
    comes from a user-editable settings file."""
    _seed(database, 'c1', 3, 5)

    hits, ids = prompt_module._context_memory_hits(
        database, '字', _settings(), limit, max_chars)

    assert isinstance(hits, list)
    assert isinstance(ids, list)


@pytest.mark.parametrize('query', ['', '   ', None, 7])
def test_context_memory_hits_reject_an_unusable_query(database, query):
    assert prompt_module._context_memory_hits(
        database, query, _settings(), 5, 500) == ([], [])


def test_context_memory_hits_without_a_database_return_nothing():
    assert prompt_module._context_memory_hits(
        None, '字', _settings(), 5, 500) == ([], [])


@pytest.mark.parametrize('limit,max_chars', [
    (None, 500), ('many', 500), (0, 500),
    (5, None), (5, 'wide'), (5, 0),
])
def test_search_memories_survives_unusable_limits(database, limit, max_chars):
    _seed(database, 'c1', 3, 5)

    assert isinstance(
        prompt_module.search_memories(database, '字', limit, max_chars), list)


def test_search_memories_without_a_database_returns_nothing():
    assert prompt_module.search_memories(None, '字') == []


def test_recent_history_without_a_database_or_conversation_returns_nothing():
    assert prompt_module._recent_history(None, 'c1', 5) == []
    assert prompt_module._recent_history(object(), '', 5) == []


def test_recent_history_survives_an_unreadable_table():
    class _Broken:
        class connection:
            @staticmethod
            def execute(*_args):
                raise sqlite3.Error('no table')

    assert prompt_module._recent_history(_Broken(), 'c1', 5) == []


def test_recent_history_skips_rows_that_are_not_user_or_assistant_text(database):
    with database.transaction() as connection:
        connection.execute("INSERT INTO conversations (id, title) VALUES ('c1', '')")
        connection.execute(
            'INSERT INTO messages (conversation_id, role, content, created_at) '
            "VALUES ('c1', 'system', '忽略', '2026-01-01T00:00:00Z')")
        connection.execute(
            'INSERT INTO messages (conversation_id, role, content, created_at) '
            "VALUES ('c1', 'user', '保留', '2026-01-01T00:00:01Z')")

    assert prompt_module._recent_history(database, 'c1', 10) == [
        {'role': 'user', 'content': '保留'}]


def test_the_style_instruction_is_empty_when_no_clause_fits(monkeypatch):
    """The style line is assembled under a hard limit; when even one clause
    cannot fit, the prompt simply carries no style line."""
    monkeypatch.setattr(prompt_module, 'STYLE_LIMIT', 1)

    assert prompt_module.style_instruction(_settings()) == ''
