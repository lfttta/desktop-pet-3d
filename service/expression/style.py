"""Deletion-only expression cleanup.

This module never adds, reorders or rephrases content. It only removes polite
openers, duplicated openings, surplus blank lines and surplus emoji, so facts
and negation relations are preserved exactly. Sentences are never split.
"""

import re

POLITE_PREFIXES = (
    '好的，', '好的,', '好的！', '好的!',
    '当然，', '当然,', '当然！', '当然!',
    '没问题，', '没问题,', '没问题！', '没问题!',
    '明白了，', '明白了,', '明白，', '明白,',
    '收到，', '收到,', '嗯，', '嗯,', '哦，', '哦,',
)

_REPEATED_OPENING = re.compile(r'^(.{1,12}?[，,。！？!?])(?:\s*\1)+')
_DUPLICATE_FIRST_SENTENCE = re.compile(r'^(.{1,80}?[。！？!?…])\s*\1')

_EMOJI_RANGES = (
    (0x1F000, 0x1FAFF),
    (0x2600, 0x27BF),
    (0x2B00, 0x2BFF),
    (0x1F1E6, 0x1F1FF),
)
_EMOJI_MODIFIERS = frozenset((0xFE0F, 0xFE0E, 0x200D, 0x20E3))
_SKIN_TONES = range(0x1F3FB, 0x1F400)


def _strip_polite_prefixes(text):
    changed = True
    while changed:
        changed = False
        for prefix in POLITE_PREFIXES:
            if text.startswith(prefix):
                text = text[len(prefix):].lstrip()
                changed = True
                break
    return text


def _is_emoji(char):
    codepoint = ord(char)
    if 0x1F3FB <= codepoint <= 0x1F3FF:
        return False
    return any(low <= codepoint <= high for low, high in _EMOJI_RANGES)


def _emoji_frequency(profile):
    if type(profile) is not dict:
        return None
    value = profile.get('emoji_frequency')
    if type(value) in (int, float) and not isinstance(value, bool):
        return float(value)
    style = profile.get('style')
    if type(style) is not dict:
        expression = profile.get('expression')
        style = expression.get('style') if type(expression) is dict else None
    if type(style) is not dict:
        return None
    value = style.get('emoji_frequency')
    if type(value) in (int, float) and not isinstance(value, bool):
        return float(value)
    return None


def _limit_emoji(text, keep):
    if keep is None:
        return text
    result = []
    kept = 0
    for char in text:
        codepoint = ord(char)
        if _is_emoji(char):
            if kept < keep:
                result.append(char)
                kept += 1
            continue
        if codepoint in _EMOJI_MODIFIERS or codepoint in _SKIN_TONES:
            continue
        result.append(char)
    return ''.join(result)


def polish(text, profile=None):
    """Return ``text`` with redundant forms and surplus emoji deleted."""
    if type(text) is not str or not text:
        return text
    result = text.replace('\r\n', '\n').replace('\r', '\n')
    result = '\n'.join(line.rstrip() for line in result.split('\n'))
    result = re.sub(r'\n{3,}', '\n\n', result)
    result = result.strip('\n')
    result = _strip_polite_prefixes(result)
    result = _DUPLICATE_FIRST_SENTENCE.sub(r'\1', result)
    result = _REPEATED_OPENING.sub(r'\1', result)
    result = _strip_polite_prefixes(result)
    frequency = _emoji_frequency(profile)
    if frequency is not None:
        # Low frequency removes all emoji; otherwise at most one survives.
        result = _limit_emoji(result, 0 if frequency <= 0.33 else 1)
    return result.strip()
