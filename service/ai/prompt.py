"""Compact prompt assembly for one chat turn.

The persona card contributes the core paragraph and the safety boundaries; a
short state summary, a few memory hits and the latest rounds follow. The whole
prompt stays within 4000 characters and the system block never loses its output
rules or boundaries. Memory, style and state are optional and are dropped in
that order when the system block runs out of room.
"""

import json
import sqlite3

from service.ai.actions import prompt_list
from service.database import SUBJECT_USER_ID
from service.persona import DEFAULT_PERSONA_ID, build_boundaries, build_prompt

MAX_PROMPT_CHARS = 4000
MAX_HISTORY_MESSAGES = 16
MAX_SYSTEM_CHARS = 2600
MAX_MEMORY_CHARS = 800
# The address line is one short sentence, not free text; the cap keeps a pasted
# paragraph in the name field from crowding out the persona.
MAX_ADDRESS_CHARS = 24
STYLE_KEYS = (
    'casualness', 'warmth', 'directness',
    'playfulness', 'sentence_length', 'emoji_frequency',
)
STYLE_CHOICES = {
    'casualness': ('用词偏正式、完整', '自然口语', '很随意，像朋友聊天'),
    'warmth': ('语气平静、克制', '温和友善', '亲切，多照顾对方感受'),
    'directness': ('先铺垫再讲重点', '直说但留有余地', '直接给结论，少绕弯'),
    'playfulness': ('稳重一点', '偶尔轻松一下', '可以俏皮一点'),
    'sentence_length': ('句子短，一次一两句', '句子长短适中', '可以稍作展开，但别啰嗦'),
    'emoji_frequency': ('不用表情符号', '最多一个表情符号', '可以偶尔用一个表情符号'),
}
STYLE_PREFIX = '表达风格：'
STYLE_CAUTION = '不得为了风格改变事实、省略关键信息或隐藏不确定性。'
STYLE_LIMIT = 220
OUTPUT_RULES = (
    '只输出一个 JSON 对象，不要 Markdown、代码块或额外解释。字段：'
    '{"text":"给用户看的回复","speak":"要朗读的短句","action":"none",'
    '"memory_candidates":[],"state_suggestion":{},"confidence":0.8}。'
    'action 只能是 none 或下列之一：' + prompt_list() + '；'
    '选和自己说的话相配的那个，不确定就填 none。'
    'memory_candidates 是最多 8 条待确认的记忆；confidence 在 0 到 1 之间。'
)


def build_messages(database, conversation_id, text, settings):
    settings = settings if isinstance(settings, dict) else {}
    memory_settings = settings.get('memory') if isinstance(settings.get('memory'), dict) else {}
    history = _recent_history(database, conversation_id, MAX_HISTORY_MESSAGES)
    memories = []
    if database is not None and memory_settings.get('enabled', True):
        limit = memory_settings.get('max_injected', 5)
        max_chars = memory_settings.get('max_entry_chars', 500)
        memories = search_memories(database, text, limit, max_chars)
    system = _system_message(settings, _state_summary(database), memories)
    current = text if isinstance(text, str) else ''

    messages = [{'role': 'system', 'content': system}]
    remaining = MAX_PROMPT_CHARS - _measure(messages) - 8
    if remaining <= 0:
        current = ''
    elif len(current) > remaining:
        current = current[-remaining:]
    remaining -= len(current)

    selected = []
    for item in reversed(history):
        cost = len(item['content']) + len(item['role']) + 4
        if cost > remaining:
            keep = remaining - len(item['role']) - 4
            if keep <= 0:
                break
            item = {'role': item['role'], 'content': item['content'][-keep:]}
            cost = len(item['content']) + len(item['role']) + 4
        selected.append(item)
        remaining -= cost
        if remaining <= 0:
            break
    selected.reverse()
    messages.extend(selected)
    messages.append({'role': 'user', 'content': current})

    # Drop history and shorten the user turn; the system block keeps its
    # output rules and boundaries intact.
    while _measure(messages) > MAX_PROMPT_CHARS and len(messages) > 2:
        messages.pop(1)
    if _measure(messages) > MAX_PROMPT_CHARS:
        room = MAX_PROMPT_CHARS - _measure(messages[:1]) - len('user') - 4
        content = messages[-1]['content']
        messages[-1]['content'] = content[-room:] if room > 0 else ''
    return messages


def _measure(messages):
    return sum(len(item['content']) + len(item['role']) + 4 for item in messages)


def _system_message(settings, state, memories):
    persona_id = settings.get('persona_id') or DEFAULT_PERSONA_ID
    core = build_prompt(persona_id).strip()
    boundaries = build_boundaries(persona_id).strip()
    address_line = _address_line(settings)
    style_line = style_instruction(settings)
    state_line = ('当前状态：' + state.strip()[:240]) if state else ''
    memory_line = ''
    if memories:
        joined = '\n'.join('- ' + item for item in memories)
        memory_line = '可能相关的记忆：\n' + joined[:MAX_MEMORY_CHARS]
    return '\n'.join(_fit_system(core, boundaries, style_line, state_line,
                                 memory_line, address_line))


def _address_line(settings):
    """How the user wants to be addressed, or "" for the default "你"."""
    name = str(settings.get('user_name') or '').strip()
    if not name or name == '你':
        return ''
    return '称呼用户为「' + name[:MAX_ADDRESS_CHARS] + '」。'


def _fit_system(core, boundaries, style_line, state_line, memory_line,
                address_line=''):
    """Drop optional sections before shrinking the core paragraph.

    Boundaries and output rules are mandatory and are never shortened.
    """

    def assemble(core_text, address_text, style_text, state_text, memory_text):
        return '\n'.join(part for part in
                         (core_text, boundaries, address_text, style_text,
                          state_text, memory_text, OUTPUT_RULES) if part)

    if len(assemble(core, address_line, style_line, state_line, memory_line)) > MAX_SYSTEM_CHARS:
        memory_line = ''
    if len(assemble(core, address_line, style_line, state_line, '')) > MAX_SYSTEM_CHARS:
        address_line = ''
    if len(assemble(core, '', style_line, state_line, '')) > MAX_SYSTEM_CHARS:
        style_line = ''
    if len(assemble(core, '', '', state_line, '')) > MAX_SYSTEM_CHARS:
        without_state = len(assemble(core, '', '', '', ''))
        room = MAX_SYSTEM_CHARS - without_state - (1 if state_line else 0)
        state_line = state_line[:max(0, room)]
    if len(assemble(core, '', '', state_line, '')) > MAX_SYSTEM_CHARS:
        without_core = len(assemble('', '', '', state_line, ''))
        room = MAX_SYSTEM_CHARS - without_core - (1 if core else 0)
        core = core[:max(0, room)]
    return [part for part in
            (core, boundaries, address_line, style_line, state_line, memory_line,
             OUTPUT_RULES)
            if part]


def _bucket(value):
    if value <= 0.33:
        return 0
    if value <= 0.66:
        return 1
    return 2


def style_instruction(settings):
    """Turn the numeric style profile into short natural-language guidance."""
    expression = settings.get('expression') if isinstance(settings, dict) else {}
    if not isinstance(expression, dict) or not expression.get('enabled', True):
        return ''
    values = expression.get('style')
    if not isinstance(values, dict):
        return ''
    clauses = []
    for key in STYLE_KEYS:
        value = values.get(key)
        if type(value) not in (int, float) or isinstance(value, bool):
            continue
        clause = STYLE_CHOICES[key][_bucket(float(value))]
        candidate = clauses + [clause]
        text = STYLE_PREFIX + '；'.join(candidate) + '。' + STYLE_CAUTION
        if len(text) <= STYLE_LIMIT:
            clauses = candidate
    if not clauses:
        return ''
    return STYLE_PREFIX + '；'.join(clauses) + '。' + STYLE_CAUTION


def _recent_history(database, conversation_id, limit):
    if database is None or not conversation_id:
        return []
    try:
        rows = database.connection.execute(
            'SELECT role, content FROM messages WHERE conversation_id=? '
            "AND subject_user_id=? AND role IN ('user','assistant') "
            'ORDER BY rowid DESC LIMIT ?',
            (conversation_id, SUBJECT_USER_ID, limit)).fetchall()
    except sqlite3.Error:
        return []
    result = []
    for role, content in reversed(rows):
        if role in ('user', 'assistant') and isinstance(content, str):
            result.append({'role': role, 'content': content})
    return result


def _state_summary(database):
    if database is None:
        return ''
    try:
        row = database.connection.execute(
            "SELECT payload FROM character_state WHERE subject_user_id='owner'"
        ).fetchone()
    except sqlite3.Error:
        return ''
    if not row or not isinstance(row[0], str):
        return ''
    try:
        payload = json.loads(row[0])
    except ValueError:
        return ''
    if not isinstance(payload, dict):
        return ''
    parts = []
    for group in ('pad', 'psych'):
        values = payload.get(group)
        if not isinstance(values, dict):
            continue
        for key, value in values.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            parts.append(key + '=' + ('%.2f' % value))
            if len(parts) >= 6:
                return '，'.join(parts)
    for key, value in payload.items():
        if key in ('pad', 'psych', 'personality', 'updated_at'):
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            continue
        parts.append(key + '=' + str(value))
        if len(parts) >= 6:
            break
    return '，'.join(parts)


def search_memories(database, query, limit=5, max_chars=500):
    """Return a small list of memory contents relevant to the query."""
    if database is None or type(query) is not str or not query.strip():
        return []
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 5
    limit = max(0, min(limit, 50))
    if limit == 0:
        return []
    try:
        max_chars = max(1, int(max_chars))
    except (TypeError, ValueError):
        max_chars = 500
    hits = _memory_search_hits(database, query, limit)
    if hits:
        return [hit[:max_chars] for hit in hits]
    rows = []
    if getattr(database, 'fts_enabled', False):
        match = _fts_query(query)
        if match:
            try:
                rows = database.connection.execute(
                    'SELECT m.content FROM memory_fts '
                    'JOIN memories AS m ON m.id = memory_fts.memory_id '
                    'WHERE memory_fts MATCH ? AND m.subject_user_id=? '
                    "AND m.status='active' AND m.deleted_at IS NULL "
                    'ORDER BY rank LIMIT ?', (match, SUBJECT_USER_ID, limit)).fetchall()
            except sqlite3.Error:
                rows = []
    if not rows:
        like = '%' + query.strip()[:120].replace('\\', '\\\\') \
            .replace('%', '\\%').replace('_', '\\_') + '%'
        try:
            rows = database.connection.execute(
                "SELECT content FROM memories WHERE deleted_at IS NULL "
                "AND subject_user_id='owner' AND status='active' "
                "AND content LIKE ? ESCAPE '\\' "
                'ORDER BY importance DESC, created_at DESC LIMIT ?',
                (like, limit)).fetchall()
        except sqlite3.Error:
            rows = []
    result = []
    for row in rows:
        content = row[0] if row else None
        if isinstance(content, str) and content:
            result.append(content[:max_chars])
    return result


def _memory_search_hits(database, query, limit):
    """Use the shared memory feature when present; stay safe otherwise."""
    try:
        from service.memory import search as memory_search
        from service.memory.store import MemoryStore
        store = getattr(database, 'memory_store_v3', None)
        if store is None:
            store = MemoryStore(database)
            setattr(database, 'memory_store_v3', store)
        results = memory_search.search(store, query, limit)
    except Exception:
        return []
    hits = []
    for item in results if isinstance(results, list) else []:
        if not isinstance(item, dict):
            continue
        text = item.get('text') or item.get('snippet') or ''
        if isinstance(text, str) and text:
            hits.append(text)
    return hits


def _fts_query(query):
    phrase = query.strip()[:80].replace('"', ' ').strip()
    if not phrase:
        return ''
    return '"' + phrase + '"'
