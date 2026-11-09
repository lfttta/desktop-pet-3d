"""Deterministic and optional model-assisted memory compaction.

A compaction creates a new version and supersedes the old one only after the
replacement text passed key-field validation. The deterministic path is always
available; model compression is opt-in through ``memory.compaction.model_compress``
and any failure leaves the queue entry retryable with the original untouched.
"""

from datetime import datetime, timezone
import json
import re

from service.database import SUBJECT_USER_ID
from service.memory import policy, writer

DEFAULT_COMPACTION = {
    'enabled': False,
    'model_compress': False,
    'daily_limit': 20,
    'min_age_days': 7,
    'max_per_run': 10,
    'protect_importance': 0.8,
}
MAX_COMPACT_LEVEL = policy.MAX_COMPACT_LEVEL
_FENCE = re.compile(r'^\s*```(?:json)?\s*(.*?)\s*```\s*$', re.DOTALL)
_MODEL_SYSTEM = (
    'You rewrite a stored memory to a lower level of detail. '
    'Level 1 normalizes formatting only. Level 2 produces a concise summary. '
    'Level 3 keeps only the key points. '
    'Never drop or change dates, times, numbers, or negations. '
    'Reply with strict JSON only: {"text": "<rewritten memory>"}.'
)


class CompactionError(Exception):
    """A compaction attempt that must return to the queue for a retry."""

    def __init__(self, code, message=''):
        super().__init__(message or code)
        self.code = code


def _moment(now=None):
    if now is None:
        return datetime.now(timezone.utc)
    if isinstance(now, datetime):
        return now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    if isinstance(now, str):
        parsed = policy.parse_timestamp(now)
        return parsed if parsed is not None else datetime.now(timezone.utc)
    if isinstance(now, (int, float)) and not isinstance(now, bool):
        return datetime.now(timezone.utc)
    return datetime.now(timezone.utc)


def compaction_config(settings):
    value = settings
    if isinstance(settings, dict):
        if isinstance(settings.get('memory'), dict):
            value = settings['memory']
        if isinstance(value, dict) and isinstance(value.get('compaction'), dict):
            value = value['compaction']
    merged = dict(DEFAULT_COMPACTION)
    if isinstance(value, dict):
        merged.update({key: item for key, item in value.items() if key in merged})
    return merged


def daily_limit(settings):
    return int(compaction_config(settings).get('daily_limit', 0))


def daily_count(store, now=None):
    moment = _moment(now)
    start = moment.astimezone(timezone.utc).strftime('%Y-%m-%dT00:00:00.000000Z')
    row = store.connection.execute(
        "SELECT COUNT(*) FROM memory_jobs WHERE job_type='compact' "
        "AND status='completed' AND completed_at >= ?", (start,)).fetchone()
    return int(row[0] or 0) if row is not None else 0


def pending_count(store):
    row = store.connection.execute(
        "SELECT COUNT(*) FROM memory_jobs WHERE job_type='compact' "
        "AND status IN ('pending','running')").fetchone()
    return int(row[0] or 0) if row is not None else 0


def daily_usage(store, now=None):
    """Completed today plus jobs already waiting, used for daily caps."""
    return daily_count(store, now) + pending_count(store)


def eligible(store, row, settings, now=None, explicit=False):
    """True when a memory may be compacted under the current policy."""
    if row is None:
        return False
    config = compaction_config(settings)
    if not config.get('enabled', False):
        return False
    if row.get('deleted_at') or row.get('status') != 'active':
        return False
    if int(row.get('protected') or 0):
        return False
    importance = float(row.get('importance') or 0.0)
    if importance >= float(config.get('protect_importance', 0.8)):
        return False
    level = policy.clamp_fuzz_level(row.get('fuzz_level'))
    if level >= MAX_COMPACT_LEVEL:
        return False
    if explicit:
        return True
    reference = policy.parse_timestamp(row.get('updated_at') or row.get('created_at'))
    if reference is None:
        return False
    moment = _moment(now)
    age = (moment - reference).total_seconds()
    return age >= int(config.get('min_age_days', 7)) * 86400


def enqueue(store, settings, memory_id=None, now=None,
            subject_user_id=SUBJECT_USER_ID):
    """Queue compaction jobs and return the protocol payload."""
    from service.memory import jobs as memory_jobs

    config = compaction_config(settings)
    if not config.get('enabled', False):
        return {'queued': 0, 'jobs': [], 'reason': 'disabled'}
    remaining = max(0, int(config.get('daily_limit', 0)) - daily_usage(store, now))
    if remaining <= 0:
        return {'queued': 0, 'jobs': [], 'reason': 'daily_limit'}

    if memory_id is not None:
        if type(memory_id) is not str:
            return {'queued': 0, 'jobs': [], 'reason': 'not_found'}
        row = store.get_memory(memory_id, subject_user_id)
        if row is None:
            return {'queued': 0, 'jobs': [], 'reason': 'not_found'}
        candidates = [row] if eligible(store, row, settings, now, explicit=True) else []
        if not candidates:
            return {'queued': 0, 'jobs': [], 'reason': 'not_eligible'}
    else:
        candidates = []
        for row in store.find_active(subject_user_id, limit=200):
            if eligible(store, row, settings, now):
                candidates.append(row)
            if len(candidates) >= int(config.get('max_per_run', 10)):
                break

    limit = min(int(config.get('max_per_run', 10)), remaining, len(candidates))
    queued = []
    for row in candidates[:limit]:
        level = policy.next_fuzz_level(row.get('fuzz_level'))
        job = memory_jobs.enqueue(store, row['id'], memory_jobs.JOB_COMPACT,
                                  {'level': level})
        if job is not None:
            queued.append(job)
    return {'queued': len(queued), 'jobs': queued}


def parse_compressed(raw, level=None):
    """Parse the strict JSON model reply; raise CompactionError on any defect."""
    if isinstance(raw, dict):
        value = raw
    elif isinstance(raw, str):
        text = raw.strip()
        fenced = _FENCE.match(text)
        if fenced:
            text = fenced.group(1).strip()
        try:
            value = json.loads(text)
        except ValueError:
            raise CompactionError('invalid_json') from None
    else:
        raise CompactionError('invalid_json')
    if not isinstance(value, dict) or set(value) - {'text', 'level'}:
        raise CompactionError('invalid_json')
    text = value.get('text')
    if type(text) is not str or not text.strip():
        raise CompactionError('invalid_text')
    if 'level' in value and level is not None:
        try:
            matches = int(value['level']) == int(level)
        except (TypeError, ValueError):
            raise CompactionError('invalid_json') from None
        if not matches:
            raise CompactionError('level_mismatch')
    return text.strip()


def build_compressor(store, ai_settings=None):
    """Return a callable ``(text, level) -> raw reply`` or None when unavailable."""
    try:
        from service.ai.provider import OpenAICompatibleProvider
        from service.secrets import load_key_for
        settings = ai_settings
        if not isinstance(settings, dict):
            source = getattr(store, 'settings', None)
            settings = source.get('ai', {}) if isinstance(source, dict) else {}
        transport = getattr(store, 'ai_transport', None)
        provider = OpenAICompatibleProvider(settings, transport=transport)
        key = load_key_for(store)
    except Exception:
        return None

    def compress(text, level):
        messages = [
            {'role': 'system', 'content': _MODEL_SYSTEM},
            {'role': 'user', 'content': 'Level: %d\nMemory:\n%s' % (level, text)},
        ]
        completion = provider.stream_chat(messages, key)
        return getattr(completion, 'text', '')

    return compress


def process_memory(store, memory_id, settings, now=None, level_limit=MAX_COMPACT_LEVEL,
                   allow_model=True, compressor=None):
    """Compact one memory; raises on failure so the job can be retried."""
    config = compaction_config(settings)
    if not config.get('enabled', False):
        return {'status': 'skipped', 'reason': 'disabled'}
    row = store.get_memory(memory_id)
    if row is None or row.get('deleted_at') or row.get('status') != 'active':
        return {'status': 'skipped', 'reason': 'not_active'}
    if int(row.get('protected') or 0):
        return {'status': 'skipped', 'reason': 'protected'}
    importance = float(row.get('importance') or 0.0)
    if importance >= float(config.get('protect_importance', 0.8)):
        return {'status': 'skipped', 'reason': 'important'}
    if daily_count(store, now) >= int(config.get('daily_limit', 0)):
        return {'status': 'skipped', 'reason': 'daily_limit'}

    old_level = policy.clamp_fuzz_level(row.get('fuzz_level'))
    text = row.get('content') or ''

    # Pick the lowest next fidelity level whose deterministic rewrite actually
    # changes the text; a no-op format pass should not block a summary pass.
    chosen = None
    target = None
    for level in range(old_level + 1, int(level_limit) + 1):
        candidate = policy.apply_fuzz(text, level)
        if candidate is None:
            continue
        if policy.normalize_text(candidate) == policy.normalize_text(text):
            continue
        chosen = candidate
        target = level
        break

    if allow_model and config.get('model_compress') and compressor is not None:
        if target is None:
            target = min(int(level_limit), max(old_level + 1, policy.FUZZ_SUMMARY))
        raw = compressor(text, target)
        parsed = parse_compressed(raw, target)
        if not policy.preserves_key_fields(text, parsed):
            raise CompactionError('key_fields_lost')
        chosen = parsed

    if chosen is None or target is None:
        return {'status': 'skipped', 'reason': 'no_safe_rewrite'}
    if policy.normalize_text(chosen) == policy.normalize_text(text):
        return {'status': 'skipped', 'reason': 'unchanged'}
    return writer.commit_compaction(store, memory_id, chosen, target)
