"""Prompt assembly and output validation for proactive cloud messages.

The scheduler in :mod:`service.behavior.proactive` decides *whether* the
character may speak; this module only builds the one-shot prompt, reads a short
conversation tail and validates the returned line. It never writes storage and
never logs message bodies or credentials.

The system block is the persona prompt, the safety boundaries and one short
rule set for speaking first. The user block carries only the current state
label, a short transcript and the instruction to say one line. Optional
memories, when supplied, are appended to the system block and never change the
user block.
"""

import re
import sqlite3

from service.ai.provider import ProviderError
from service.database import SUBJECT_USER_ID
from service.persona import DEFAULT_PERSONA_ID, build_boundaries, build_prompt

MAX_PROACTIVE_CHARS = 120
DEFAULT_HISTORY_LIMIT = 2
MAX_TRANSCRIPT_CHARS = 200
MAX_MEMORY_CHARS = 400

PROACTIVE_RULES = (
    '现在你要主动开口：只说一句 8 到 40 个字的中文，不提问，也不要求对方回应；'
    '不提及屏幕、窗口或你观察到的任何情况；不编造事实、经历或数据；'
    '不要提到 JSON、格式或重试。'
)

# Phrases that would expose how the desktop state was obtained.
FORBIDDEN_TOKENS = ('屏幕', '窗口', '我注意到你', '检测到你在', '监控')
# English state labels and English observation nouns are as revealing as the
# Chinese ones, so the check is case-insensitive and covers both scripts.
FORBIDDEN_ASCII_TOKENS = (
    'json',
    'fullscreen', 'full_screen', 'do_not_disturb', 'dnd',
    'sleep_hours', 'active_interaction', 'active_work',
    'desktop', 'screenshot', 'monitor', 'window', 'screen', 'idle',
    'i noticed', 'i see you',
)

_WRAPPING_QUOTES = {
    '"': '"',
    "'": "'",
    '“': '”',
    '‘': '’',
    '「': '」',
    '『': '』',
}
_WHITESPACE = re.compile(r'\s+')


def _persona_id(persona_id, settings):
    if isinstance(persona_id, str) and persona_id:
        return persona_id
    configured = settings.get('persona_id')
    if isinstance(configured, str) and configured:
        return configured
    return DEFAULT_PERSONA_ID


def _transcript(history):
    lines = []
    for item in history if isinstance(history, list) else []:
        if not isinstance(item, dict):
            continue
        role = item.get('role')
        content = item.get('content')
        if role not in ('user', 'assistant'):
            continue
        if type(content) is not str or not content.strip():
            continue
        prefix = '对方：' if role == 'user' else '你：'
        lines.append(prefix + content.strip()[:MAX_TRANSCRIPT_CHARS])
    return '\n'.join(lines)


def _memory_lines(memories):
    entries = []
    for item in memories if isinstance(memories, list) else []:
        if not isinstance(item, str) or not item.strip():
            continue
        entries.append('- ' + item.strip()[:MAX_TRANSCRIPT_CHARS])
    if not entries:
        return ''
    return '\n'.join(entries)[:MAX_MEMORY_CHARS]


def build_messages(settings, persona_id, state_label, history, memories=None):
    """Return the two-message prompt used for one proactive generation."""
    settings = settings if isinstance(settings, dict) else {}
    persona_id = _persona_id(persona_id, settings)
    system_parts = [
        build_prompt(persona_id).strip(),
        build_boundaries(persona_id).strip(),
        PROACTIVE_RULES,
    ]
    memory_text = _memory_lines(memories)
    if memory_text:
        system_parts.append('可能记得的事：\n' + memory_text)
    system = '\n'.join(part for part in system_parts if part)

    user_parts = []
    label = state_label.strip() if isinstance(state_label, str) else ''
    if label:
        user_parts.append('当前状态：' + label)
    transcript = _transcript(history)
    if transcript:
        user_parts.append('最近的对话：\n' + transcript)
    user_parts.append('现在主动说一句。')
    return [
        {'role': 'system', 'content': system},
        {'role': 'user', 'content': '\n'.join(user_parts)},
    ]


def _strip_wrapping_quotes(value):
    while len(value) >= 2:
        closing = _WRAPPING_QUOTES.get(value[0])
        if closing is None or value[-1] != closing:
            break
        value = value[1:-1].strip()
    return value


def validate_text(text):
    """Return the cleaned line, or None when it must not be shown.

    Leading and trailing whitespace and one or more layers of wrapping quotes
    are removed, whitespace runs collapse to a single space, and the result
    must be 1..120 characters without any observation-related wording.
    """
    if type(text) is not str:
        return None
    value = _strip_wrapping_quotes(text.strip())
    value = _WHITESPACE.sub(' ', value).strip()
    if not value or len(value) > MAX_PROACTIVE_CHARS:
        return None
    for token in FORBIDDEN_TOKENS:
        if token in value:
            return None
    folded = value.casefold()
    for token in FORBIDDEN_ASCII_TOKENS:
        if token in folded:
            return None
    return value


def recent_history(database, limit=2):
    """Return the tail of the most recently active conversation, oldest first.

    Reads only the shared SQLite connection; any storage failure yields an
    empty list so the prompt simply loses its context.
    """
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = DEFAULT_HISTORY_LIMIT
    if limit <= 0 or database is None:
        return []
    connection = getattr(database, 'connection', None)
    if connection is None:
        return []
    try:
        latest = connection.execute(
            'SELECT conversation_id FROM messages WHERE subject_user_id=? '
            "AND role IN ('user','assistant') ORDER BY rowid DESC LIMIT 1",
            (SUBJECT_USER_ID,)).fetchone()
        if latest is None or not latest[0]:
            return []
        rows = connection.execute(
            'SELECT role, content FROM messages WHERE conversation_id=? '
            "AND subject_user_id=? AND role IN ('user','assistant') "
            'ORDER BY rowid DESC LIMIT ?',
            (latest[0], SUBJECT_USER_ID, limit)).fetchall()
    except (AttributeError, TypeError, sqlite3.Error):
        return []
    result = []
    for role, content in reversed(rows):
        if role not in ('user', 'assistant'):
            continue
        if type(content) is not str or not content.strip():
            continue
        result.append({'role': role, 'content': content})
    return result


def generate(provider, key, messages, cancel_event=None):
    """Run one provider call and normalize the result.

    ``ProviderError`` and any unexpected provider failure become ``None`` so
    the scheduler can fall back to a local phrase.
    """
    try:
        completion = provider.stream_chat(messages, key, on_delta=None,
                                          cancel_event=cancel_event)
    except ProviderError:
        return None
    except Exception:
        return None
    text = getattr(completion, 'text', '')
    if type(text) is not str:
        return None
    usage = getattr(completion, 'usage', {})
    if not isinstance(usage, dict):
        usage = {}
    return {
        'text': text,
        'usage': usage,
        'model': str(getattr(completion, 'model', '') or ''),
    }
