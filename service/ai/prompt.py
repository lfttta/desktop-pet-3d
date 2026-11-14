"""Compact prompt assembly for one chat turn.

The persona card contributes the core paragraph and the safety boundaries; a
short state summary, an optional earlier-conversation summary, a few memory hits
and the latest rounds follow. The whole prompt stays within 6000 characters and
the system block never loses its output rules or boundaries. Memory, summary,
address, style and tone are optional and are dropped in that order when the
system block runs out of room.
"""

import json
import sqlite3

from service.ai.actions import prompt_list
from service.database import SUBJECT_USER_ID
from service.persona import DEFAULT_PERSONA_ID, build_boundaries, build_prompt
from service.persona_tone import DEFAULT_TONE, TONE_DIRECTIVES
from service.state.character import GROWABLE_TRAITS

try:  # The summarizer is optional; a service without it keeps the old prompt.
    from service.ai.summary import load_summary
except ImportError:  # pragma: no cover - exercised through the missing module
    load_summary = None

MAX_PROMPT_CHARS = 6000
MAX_HISTORY_MESSAGES = 24
# The system block stays well below MAX_PROMPT_CHARS so history still fits; the
# memory allowance lives inside this block, not on top of it.
MAX_SYSTEM_CHARS = 4000
MAX_MEMORY_CHARS = 1600
MAX_SUMMARY_CHARS = 800
MAX_STATE_ITEMS = 6
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
    'memory_candidates 是最多 8 条待确认的记忆，每条可以是一个字符串，'
    '也可以是 {"text":"待记住的内容","category":"general|preference|relationship|feedback",'
    '"importance":0.5,"source_type":"chat|correction|manual|import|system|compaction"}；'
    '只有用户明确表达偏好、关系变化或反馈时才使用非 general 的分类。'
    'confidence 在 0 到 1 之间。'
)


def build_messages(database, conversation_id, text, settings, settings_source=None):
    settings = settings if isinstance(settings, dict) else {}
    memory_settings = settings.get('memory') if isinstance(settings.get('memory'), dict) else {}
    history = _recent_history(database, conversation_id, MAX_HISTORY_MESSAGES)
    memories = []
    memory_ids = []
    if database is not None and memory_settings.get('enabled', True):
        limit = memory_settings.get('max_injected', 5)
        max_chars = memory_settings.get('max_entry_chars', 500)
        memories, memory_ids = _context_memory_hits(
            database, text, settings, limit, max_chars, settings_source)
    summary = _conversation_summary(database, conversation_id)
    system, injected = _system_message(settings, _state_summary(database),
                                       memories, summary)
    if memory_ids and injected:
        _record_memory_recall(
            database, [memory_ids[index] for index in injected
                       if 0 <= index < len(memory_ids)])
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


def _system_message(settings, state, memories, summary=''):
    persona_id = settings.get('persona_id') or DEFAULT_PERSONA_ID
    core = build_prompt(persona_id).strip()
    boundaries = build_boundaries(persona_id).strip()
    address_line = _address_line(settings)
    style_line = style_instruction(settings)
    tone_line = tone_instruction(settings)
    state_line = ('当前状态：' + state.strip()[:240]) if state else ''
    summary_line = ''
    if summary:
        summary_line = '更早的对话摘要：' + summary.strip()[:MAX_SUMMARY_CHARS]
    memory_line = ''
    injected = []
    if memories:
        memory_line, injected = _memory_line(memories)
    parts = _fit_system(core, boundaries, style_line, tone_line,
                        state_line, summary_line, memory_line, address_line)
    if not memory_line or memory_line not in parts:
        injected = []
    return '\n'.join(parts), injected


def _memory_line(memories):
    """Render memory bullets that fit MAX_MEMORY_CHARS and list their indexes.

    The indexes are the memories that actually enter the prompt; the caller
    counts only those as recalled so a budget-dropped hit never strengthens its
    own ranking.
    """
    parts = []
    indexes = []
    used = 0
    for index, item in enumerate(memories):
        if type(item) is not str or not item:
            continue
        line = '- ' + item
        cost = len(line) + (1 if parts else 0)
        if used + cost > MAX_MEMORY_CHARS:
            break
        parts.append(line)
        indexes.append(index)
        used += cost
    if not parts:
        return '', []
    return '可能相关的记忆：\n' + '\n'.join(parts), indexes


def _record_memory_recall(database, memory_ids):
    if database is None or not memory_ids:
        return
    try:
        from service.memory.search import record_recall
    except (ImportError, AttributeError):
        return
    try:
        record_recall(database, memory_ids)
    except Exception:
        pass


def tone_instruction(settings):
    """Return the one-sentence speaking-tone directive, or "" for custom."""
    expression = settings.get('expression') if isinstance(settings, dict) else None
    tone = expression.get('tone') if isinstance(expression, dict) else None
    if type(tone) is not str or not tone:
        tone = DEFAULT_TONE
    return TONE_DIRECTIVES.get(tone, '')


def _address_line(settings):
    """How the user wants to be addressed, or "" for the default "你"."""
    name = str(settings.get('user_name') or '').strip()
    if not name or name == '你':
        return ''
    return '称呼用户为「' + name[:MAX_ADDRESS_CHARS] + '」。'


def _fit_system(core, boundaries, style_line, tone_line, state_line,
                summary_line, memory_line, address_line=''):
    """Drop optional sections before shrinking the core paragraph.

    Boundaries and output rules are mandatory and are never shortened. The drop
    order keeps the facts (state) longer than the phrasing hints (style, tone)
    and the older summary longer than the raw memory hits.
    """

    def assemble(core_text, address_text, style_text, tone_text, state_text,
                 summary_text, memory_text):
        return '\n'.join(part for part in
                         (core_text, boundaries, address_text, style_text,
                          tone_text, state_text, summary_text, memory_text,
                          OUTPUT_RULES) if part)

    if len(assemble(core, address_line, style_line, tone_line, state_line,
                    summary_line, memory_line)) > MAX_SYSTEM_CHARS:
        memory_line = ''
    if len(assemble(core, address_line, style_line, tone_line, state_line,
                    summary_line, '')) > MAX_SYSTEM_CHARS:
        summary_line = ''
    if len(assemble(core, address_line, style_line, tone_line, state_line,
                    '', '')) > MAX_SYSTEM_CHARS:
        address_line = ''
    if len(assemble(core, '', style_line, tone_line, state_line,
                    '', '')) > MAX_SYSTEM_CHARS:
        style_line = ''
    if len(assemble(core, '', '', tone_line, state_line,
                    '', '')) > MAX_SYSTEM_CHARS:
        tone_line = ''
    if len(assemble(core, '', '', '', state_line, '', '')) > MAX_SYSTEM_CHARS:
        without_state = len(assemble(core, '', '', '', '', '', ''))
        room = MAX_SYSTEM_CHARS - without_state - (1 if state_line else 0)
        state_line = state_line[:max(0, room)]
    if len(assemble(core, '', '', '', state_line, '', '')) > MAX_SYSTEM_CHARS:
        without_core = len(assemble('', '', '', '', state_line, '', ''))
        room = MAX_SYSTEM_CHARS - without_core - (1 if core else 0)
        core = core[:max(0, room)]
    return [part for part in
            (core, boundaries, address_line, style_line, tone_line, state_line,
             summary_line, memory_line, OUTPUT_RULES)
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


def _compact_number(value):
    """Format a state number without trailing zeros (0.5, not 0.50)."""
    text = ('%.2f' % value).rstrip('0').rstrip('.')
    return text if text not in ('', '-0') else '0'


def _trait_item(payload):
    """Return the persisted growable traits as one compact ``traits=`` item.

    Only growable traits can be moved by an approved growth candidate, so they
    are the ones worth showing; the core card traits are fixed and stay out.
    """
    personality = payload.get('personality')
    traits = personality.get('traits') if isinstance(personality, dict) else None
    if not isinstance(traits, dict):
        # Accept a flat personality block written by older builds.
        traits = personality
    if not isinstance(traits, dict):
        return ''
    entries = []
    for trait in GROWABLE_TRAITS:
        value = traits.get(trait)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        entries.append(trait + ':' + _compact_number(float(value)))
    return ('traits=' + ','.join(entries)) if entries else ''


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
    # The trait item counts against the same six-item cap as pad and psych, so
    # it is added first; one compact item keeps the mood values visible too.
    trait_item = _trait_item(payload)
    if trait_item:
        parts.append(trait_item)
    for group in ('pad', 'psych'):
        values = payload.get(group)
        if not isinstance(values, dict):
            continue
        for key, value in values.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            parts.append(key + '=' + ('%.2f' % value))
            if len(parts) >= MAX_STATE_ITEMS:
                return '，'.join(parts)
    for key, value in payload.items():
        if key in ('pad', 'psych', 'personality', 'updated_at'):
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            continue
        parts.append(key + '=' + str(value))
        if len(parts) >= MAX_STATE_ITEMS:
            break
    return '，'.join(parts)


def _conversation_summary(database, conversation_id):
    """Return the earlier-conversation summary, or "" when unavailable."""
    loader = load_summary
    if loader is None or database is None:
        return ''
    try:
        value = loader(database, conversation_id)
    except TypeError:
        # A one-argument loader is still usable; anything else is skipped.
        try:
            value = loader(database)
        except Exception:
            return ''
    except Exception:
        return ''
    if isinstance(value, dict):
        value = value.get('summary') or value.get('text') or ''
    if type(value) is not str:
        return ''
    return value.strip()[:MAX_SUMMARY_CHARS]


def _context_memory_hits(database, query, settings, limit, max_chars,
                         settings_source=None):
    """Return ``(texts, ids)`` from the context search or the LIKE/FTS fallback.

    ``max_chars`` is the per-entry cap; the total memory allowance is
    :data:`MAX_MEMORY_CHARS`. ``settings_source`` is the SettingsStore when the
    caller has one, so the search can reach the embedding transport and key.
    """
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 5
    limit = max(0, min(limit, 50))
    if limit == 0:
        return [], []
    try:
        max_chars = max(1, int(max_chars))
    except (TypeError, ValueError):
        max_chars = 500
    if database is None or type(query) is not str or not query.strip():
        return [], []
    source = settings_source if settings_source is not None else settings
    try:
        from service.memory.search import search_for_context
        results = search_for_context(
            database, query, source, limit,
            total_chars=MAX_MEMORY_CHARS, entry_chars=max_chars, with_ids=True)
    except (ImportError, AttributeError):
        return search_memories(database, query, limit, max_chars), []
    ids = []
    if isinstance(results, tuple) and len(results) == 2:
        results, raw_ids = results
        ids = raw_ids if isinstance(raw_ids, list) else []
    hits = []
    hit_ids = []
    for position, item in enumerate(results if isinstance(results, list) else []):
        if isinstance(item, dict):
            text = item.get('text') or item.get('snippet') or item.get('content') or ''
        else:
            text = item if type(item) is str else ''
        if not text:
            continue
        hits.append(text[:max_chars])
        hit_ids.append(ids[position] if position < len(ids) else None)
        if len(hits) >= limit:
            break
    if not hits:
        # The context search can come back empty while the raw LIKE/FTS path
        # still matches (for example before the index is rebuilt).
        return search_memories(database, query, limit, max_chars), []
    return hits, hit_ids


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
