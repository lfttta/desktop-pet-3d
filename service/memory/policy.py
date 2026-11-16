"""Strength scoring and short-window duplicate policy for memories.

Scores are deliberately simple and deterministic: importance dominates,
repetition adds saturated value, and confidence covers how sure the source was.
Nothing here reads or writes storage.
"""

from datetime import datetime, timedelta, timezone
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


# -- fact occurrence time -------------------------------------------------
#
# 事实发生时间（occurred_at）的最小确定性解析器。依据：Graphiti/Zep 把每条
# 事实的世界时间线（valid_at）与系统时间线（created_at）分开，检索排序应使用
# 世界时间；Mem0 以对话发生日（Observation Date）为锚解析相对表达
# （'User went to Paris last week is useless 6 months later'），decay 在缺失
# event_date 时回退 updated_at。本仓库取其最小子集：只认下面这个封闭集合，
# 解析不出一律返回 None（写入侧落 NULL，读取侧回退 updated_at），绝不猜
# （Graphiti extract_timestamps 的 'NEVER hallucinate dates' 原则）。
#
# 已知边界（均确定性、fail-open，留待后续轮次）：
# - 「中午1点」按 01:00 原样解析（口语的 13:00 与 11:00 有歧义，不猜）；
# - 「去年 3 月」只有月没有日：不猜具体日，仍返回 None；带日的
#   「去年3月15日」按紧邻的年份前缀修正锚定年；
# - URL/表格里的日期会被当作事实时间（排除它们需要分词级上下文，超出本轮）；
# - 日界按 UTC，与仓库全 UTC 约定一致。

_OCCURRENCE_DATE_FULL = re.compile(
    r'(?<!\d)(\d{4})\s*[-/年.]\s*(\d{1,2})\s*[-/月.]\s*(\d{1,2})\s*[日号]?(?!\d)')
_OCCURRENCE_DATE_SHORT = re.compile(r'(?<!\d)(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]')
# 冒号时刻是 24 小时制；点/时时刻支持上午/下午/凌晨等前缀修正。
_OCCURRENCE_CLOCK_COLON = re.compile(r'(?<!\d)(\d{1,2})[:：](\d{2})(?!\d)')
_OCCURRENCE_CLOCK_DIAN = re.compile(
    r'(上午|中午|下午|晚上|凌晨|早上|傍晚)?\s*(\d{1,2})\s*[点时]\s*'
    r'(?:(\d{1,2})\s*分?)?')
# 相对表达只做这一组，以写入时刻为锚。(?<![大前]) 挡住「大后天」，(?<!\d)
# 挡住「1000天后」尾随命中「0天后」——不在支持集合内时宁可整体放弃。
_OCCURRENCE_RELATIVE = re.compile(
    r'今天|今日|明天|明日|(?<![大前])后天|(?<!\d)(\d{1,3})\s*天[后後]'
    r'|下周([一二三四五六日天])')
_AFTERNOON_PREFIXES = ('下午', '晚上', '傍晚')
_WEEKDAY_INDEX = {'一': 0, '二': 1, '三': 2, '四': 3, '五': 4, '六': 5,
                  '日': 6, '天': 6}
# 紧邻日期的相对年份前缀；长的放前面，避免「大前年」被「前年」截走。
_YEAR_PREFIXES = (('大前年', -3), ('大后年', 3), ('前年', -2), ('后年', 2),
                  ('去年', -1), ('今年', 0), ('明年', 1))


def _relative_year_offset(text, start):
    """Return the year offset of a 去年/今年/明年 token right before ``start``.

    Only whitespace may sit between the token and the date; a prefix earlier in
    the sentence is ignored so ``去年我去了北京，3月15日回来`` does not shift
    the date by a year. No token means the anchor year (the pre-v11 behavior).
    """
    head = text[:start].rstrip()
    for token, offset in _YEAR_PREFIXES:
        if head.endswith(token):
            return offset
    return 0


def _occurrence_date(text, anchor):
    for match in _OCCURRENCE_DATE_FULL.finditer(text):
        year = int(match.group(1))
        # 年份远离锚点的多为 URL/编号/表格碎片（语料实测出现过
        # 9255-09-27 这类解析），宁可放弃。
        if not 1900 <= year <= anchor.year + 100:
            continue
        try:
            return datetime(year, int(match.group(2)),
                            int(match.group(3)), tzinfo=timezone.utc).date()
        except ValueError:
            continue
    # 无年份的月日：先用紧邻的「去年/今年/明年」修正锚定年；没有前缀时取
    # 锚定年，不做进退年猜测。
    for match in _OCCURRENCE_DATE_SHORT.finditer(text):
        year = anchor.year + _relative_year_offset(text, match.start())
        try:
            return datetime(year, int(match.group(1)),
                            int(match.group(2)), tzinfo=timezone.utc).date()
        except ValueError:
            continue
    return None


def _occurrence_relative_day(text, anchor):
    for match in _OCCURRENCE_RELATIVE.finditer(text):
        if match.group(1) is not None:
            return anchor.date() + timedelta(days=int(match.group(1)))
        if match.group(2) is not None:
            # 周一为一周之首：下周X = 本周剩余天数 + X 的序数。
            return anchor.date() + timedelta(
                days=(7 - anchor.weekday()) + _WEEKDAY_INDEX[match.group(2)])
        word = match.group()
        if word in ('今天', '今日'):
            return anchor.date()
        if word in ('明天', '明日'):
            return anchor.date() + timedelta(days=1)
        if word == '后天':
            return anchor.date() + timedelta(days=2)
    return None


def _occurrence_clock(text):
    """Return (hour, minute) from the earliest parseable clock token."""
    candidates = []
    for match in _OCCURRENCE_CLOCK_COLON.finditer(text):
        hour, minute = int(match.group(1)), int(match.group(2))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            candidates.append((match.start(), hour, minute))
            break
    for match in _OCCURRENCE_CLOCK_DIAN.finditer(text):
        hour = int(match.group(2))
        if match.group(1) in _AFTERNOON_PREFIXES and hour < 12:
            hour += 12
        minute = int(match.group(3) or 0)
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            candidates.append((match.start(), hour, minute))
            break
    if not candidates:
        return None
    _, hour, minute = min(candidates)
    return hour, minute


def occurred_at_for(text, now_str):
    """Parse the fact time a memory text points at; None when unparseable.

    纯函数：``text`` 为记忆原文，``now_str`` 为写入时刻（created_at），
    相对表达（今天/明天/后天/N天后/下周X）以它为锚。日期只给到日时取当日
    00:00 UTC；绝对日期与相对词同时出现时绝对日期优先；只有时刻没有日期时
    取写入当天。只认上方的封闭集合，其余一律返回 None（写入侧落 NULL，
    读取侧回退 updated_at）。
    """
    anchor = parse_timestamp(now_str)
    if anchor is None:
        return None
    if type(text) is not str or not text.strip():
        return None
    day = _occurrence_date(text, anchor)
    if day is None:
        day = _occurrence_relative_day(text, anchor)
    clock = _occurrence_clock(text)
    if day is None and clock is None:
        return None
    if day is None:
        day = anchor.date()
    hour, minute = clock if clock is not None else (0, 0)
    try:
        moment = datetime(day.year, day.month, day.day, hour, minute,
                          tzinfo=timezone.utc)
    except ValueError:
        return None
    return moment.strftime('%Y-%m-%dT%H:%M:%S.%fZ')


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
