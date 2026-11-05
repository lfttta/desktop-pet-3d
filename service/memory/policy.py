"""Strength scoring and short-window duplicate policy for memories.

Scores are deliberately simple and deterministic: importance dominates,
repetition adds saturated value, and confidence covers how sure the source was.
Nothing here reads or writes storage.
"""

from datetime import datetime, timezone
import re
import unicodedata

DEDUPE_WINDOW_SECONDS = 600
DEFAULT_CONFIDENCE = 0.5
DEDUPE_THRESHOLD = 0.7
CONTAINMENT_RATIO = 0.5

# Fuzz levels describe how much detail a memory body still carries.
FUZZ_EXACT = 0        # original wording
FUZZ_FORMAT = 1       # whitespace and formatting cleanup only
FUZZ_SUMMARY = 2      # compressed summary
FUZZ_SKELETON = 3     # key points only
FUZZ_ARCHIVED = 4     # body moved out of the active store
FUZZ_LEVELS = (FUZZ_EXACT, FUZZ_FORMAT, FUZZ_SUMMARY, FUZZ_SKELETON, FUZZ_ARCHIVED)
MAX_COMPACT_LEVEL = FUZZ_SKELETON
SUMMARY_LIMIT = 200
SKELETON_LIMIT = 100

# \w keeps CJK characters, so only whitespace, symbols and punctuation are removed.
_NOISE = re.compile(r'[\s\W_]+', re.UNICODE)
_WHITESPACE = re.compile(r'[ \t\f\v]+')
_BLANK_LINES = re.compile(r'\n{3,}')
_SENTENCE_END = re.compile(r'([^。！？!?；;\n]*[。！？!?；;\n]?)')
_NUMBER = re.compile(r'(?<!\d)(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?%?')
_TIME_PATTERNS = (
    re.compile(r'\d{4}[-/年.]\d{1,2}[-/月.]\d{1,2}\s*[日号]?'),
    re.compile(r'\d{1,2}月\d{1,2}\s*[日号]'),
    re.compile(r'(?<!\d)\d{1,2}[:：]\d{2}(?!\d)'),
    re.compile(r'(?<!\d)\d{1,2}\s*[点时](?:\s*\d{1,2}\s*分?)?'),
)
_NEGATION_WORDS = (
    '不', '没', '没有', '别', '勿', '未', '无', '非',
    'not', 'no', 'never', 'without', 'none', 'cannot',
    "don't", "doesn't", "didn't", "isn't", "aren't", "won't",
    "can't", "couldn't", "shouldn't", "wouldn't",
)
_NEGATION_EN = re.compile(
    r'(?<![A-Za-z])(' + '|'.join(
        re.escape(word) for word in _NEGATION_WORDS if word.isascii()) + r')(?![A-Za-z])',
    re.IGNORECASE)
_NEGATION_CJK = tuple(word for word in _NEGATION_WORDS if not word.isascii())


def clamp(value, low=0.0, high=1.0):
    return max(low, min(high, float(value)))


def normalize_text(text):
    """Case-folded, punctuation-free form used for equality and topic checks."""
    if type(text) is not str:
        return ''
    normalized = unicodedata.normalize('NFKC', text).casefold()
    return _NOISE.sub('', normalized)


def _saturate(count):
    count = max(0, int(count))
    return 1.0 - 1.0 / (1.0 + count)


def strength(importance, mention_count=1, session_count=1, recall_count=0,
             confidence=DEFAULT_CONFIDENCE):
    """Weighted strength in [0, 1] from the five stored signal counters."""
    mention = _saturate(mention_count)
    session = _saturate(session_count)
    recall = _saturate(recall_count)
    value = (0.40 * clamp(importance)
             + 0.20 * mention
             + 0.15 * session
             + 0.15 * recall
             + 0.10 * clamp(confidence))
    return round(clamp(value), 6)


def merge_importance(current, incoming, mention_count):
    """Repeated appearances move importance toward the incoming value with a
    weight that shrinks as mention_count grows."""
    current = clamp(current)
    incoming = clamp(incoming)
    weight = 1.0 / (max(1, int(mention_count)) + 1)
    return round(clamp(current + (incoming - current) * weight), 6)


def merge_confidence(current, incoming, mention_count):
    current = clamp(current)
    incoming = clamp(incoming)
    weight = 1.0 / (max(1, int(mention_count)) + 1)
    return round(clamp(current + (incoming - current) * weight), 6)


def _bigrams(text):
    normalized = normalize_text(text)
    if len(normalized) < 2:
        return {normalized} if normalized else set()
    return {normalized[index:index + 2] for index in range(len(normalized) - 1)}


def similarity(first, second):
    """Character-bigram Jaccard similarity of the normalized texts."""
    left = _bigrams(first)
    right = _bigrams(second)
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def is_same_topic(first, second, threshold=DEDUPE_THRESHOLD):
    if type(first) is not str or type(second) is not str:
        return False
    left = normalize_text(first)
    right = normalize_text(second)
    if not left or not right:
        return False
    if left == right:
        return True
    shorter, longer = (left, right) if len(left) <= len(right) else (right, left)
    if shorter in longer and len(shorter) / len(longer) >= CONTAINMENT_RATIO:
        return True
    return similarity(left, right) >= threshold


def parse_timestamp(value):
    if type(value) is not str or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def within_window(value, now=None, seconds=DEDUPE_WINDOW_SECONDS):
    """True when value is a parseable timestamp no older than seconds."""
    parsed = parse_timestamp(value)
    if parsed is None:
        return False
    current = now if now is not None else datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    delta = (current - parsed).total_seconds()
    return 0 <= delta <= seconds


def timestamp_value(value):
    parsed = parse_timestamp(value)
    return parsed.timestamp() if parsed is not None else 0.0


# -- fuzz levels ---------------------------------------------------------


def clamp_fuzz_level(level):
    try:
        value = int(level)
    except (TypeError, ValueError):
        return FUZZ_EXACT
    return max(FUZZ_EXACT, min(FUZZ_ARCHIVED, value))


def next_fuzz_level(level):
    return min(MAX_COMPACT_LEVEL, clamp_fuzz_level(level) + 1)


# -- topic keys ----------------------------------------------------------


def topic_key(text, limit=64):
    """Deterministic coarse topic key for grouping related memories."""
    normalized = normalize_text(text)
    return normalized[:limit]


# -- key field extraction ------------------------------------------------


def _numbers(text):
    return sorted({match.group() for match in _NUMBER.finditer(text)})


def _times(text):
    found = set()
    for pattern in _TIME_PATTERNS:
        for match in pattern.finditer(text):
            value = match.group().strip()
            if value:
                found.add(value)
    return sorted(found)


def _negations(text):
    folded = text.casefold()
    found = {word for word in _NEGATION_CJK if word in folded}
    found.update(match.group(1) for match in _NEGATION_EN.finditer(folded))
    return sorted(found)


def key_fields(text):
    """Return the time, number and negation tokens that must survive a rewrite."""
    if type(text) is not str or not text:
        return {'times': [], 'numbers': [], 'negations': []}
    return {'times': _times(text), 'numbers': _numbers(text),
            'negations': _negations(text)}


def _number_present(token, text):
    return re.search(r'(?<![\d.])' + re.escape(token) + r'(?![\d.])', text) is not None


def missing_key_fields(original, candidate):
    """List of ``kind:value`` tokens present in original but absent in candidate."""
    if type(candidate) is not str:
        candidate = ''
    fields = key_fields(original)
    missing = []
    for token in fields['times']:
        if token not in candidate:
            missing.append('time:' + token)
    for token in fields['numbers']:
        if not _number_present(token, candidate):
            missing.append('number:' + token)
    folded = candidate.casefold()
    for token in fields['negations']:
        if token.isascii():
            present = re.search(r'(?<![A-Za-z])' + re.escape(token) + r'(?![A-Za-z])',
                                folded) is not None
        else:
            present = token in folded
        if not present:
            missing.append('negation:' + token)
    return missing


def preserves_key_fields(original, candidate):
    return not missing_key_fields(original, candidate)


# -- deterministic rewrites ---------------------------------------------


def format_clean(text):
    """Normalize whitespace without changing wording or punctuation."""
    if type(text) is not str:
        return None
    normalized = unicodedata.normalize('NFKC', text)
    lines = [_WHITESPACE.sub(' ', line).strip() for line in normalized.splitlines()]
    collapsed = '\n'.join(lines)
    return _BLANK_LINES.sub('\n\n', collapsed).strip()


def _cut_at_sentence(text, limit):
    window = text[:limit]
    for separator in ('。', '！', '？', '.', '!', '?', '；', ';', '\n'):
        index = window.rfind(separator)
        if index >= max(1, limit // 3):
            return index + 1
    return limit


def summarize(text, limit=SUMMARY_LIMIT):
    """Deterministic summary; None when a key field would be dropped."""
    cleaned = format_clean(text)
    if not cleaned:
        return None
    if len(cleaned) <= limit:
        return cleaned
    candidate = cleaned[:_cut_at_sentence(cleaned, limit)].rstrip()
    if not candidate or not preserves_key_fields(cleaned, candidate):
        return None
    return candidate + '…'


def skeleton(text, limit=SKELETON_LIMIT):
    """Deterministic key-point form; None when a key field would be dropped."""
    cleaned = format_clean(text)
    if not cleaned:
        return None
    if len(cleaned) <= limit:
        return cleaned
    sentences = [part for part in _SENTENCE_END.findall(cleaned) if part]
    selected = []
    for sentence in sentences:
        addition = sentence if not selected else sentence
        total = len(''.join(selected))
        if total + len(addition) > limit and selected:
            break
        selected.append(addition)
    candidate = ''.join(selected).strip()
    if not candidate:
        return None
    # Add any key-bearing sentence that did not fit in the leading budget.
    if not preserves_key_fields(cleaned, candidate):
        for sentence in sentences:
            if not preserves_key_fields(cleaned, candidate + sentence):
                candidate = (candidate + sentence).strip()
                if preserves_key_fields(cleaned, candidate):
                    break
    if not preserves_key_fields(cleaned, candidate):
        return None
    return candidate + '…'


def apply_fuzz(text, level):
    """Return the rewritten text for a target level, or None if unsafe."""
    level = clamp_fuzz_level(level)
    if level == FUZZ_EXACT:
        candidate = text
    elif level == FUZZ_FORMAT:
        candidate = format_clean(text)
    elif level == FUZZ_SUMMARY:
        candidate = summarize(text)
    elif level == FUZZ_SKELETON:
        candidate = skeleton(text)
    else:
        return None
    if type(candidate) is not str or not candidate.strip():
        return None
    if not preserves_key_fields(text, candidate):
        return None
    return candidate
