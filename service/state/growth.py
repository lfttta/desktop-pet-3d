"""Personality growth: evidence collection, candidates, approval and rollback.

The model never writes personality values. Repeated explicit feedback or
preference/relationship memories become evidence; a candidate is only created
when the session and distinct-memory thresholds are met, the trait is not
locked, the same evidence was not already decided and the per-trait cooldown
has elapsed. Approval applies at most ``growth.max_step`` and records an
undoable row in ``personality_history``.
"""

from datetime import datetime, timezone
import json
import re
import unicodedata
import uuid

from service.database import SUBJECT_USER_ID
from service.state import character

RULE_VERSION = 1
STEP_PER_EVENT = 0.02
CANDIDATE_LIMIT = 50
HISTORY_LIMIT = 50

DEFAULT_SETTINGS = {
    'enabled': False,
    'min_sessions': 3,
    'min_memories': 2,
    'max_step': 0.05,
    'cooldown_days': 7,
    'locked_traits': [],
}

# Only explicit feedback and repeated preference/relationship memories count.
ELIGIBLE_CATEGORIES = frozenset({'preference', 'relationship', 'feedback'})
ELIGIBLE_SOURCE_TYPES = frozenset({'correction'})

_NOISE = re.compile(r'[\s\W_]+', re.UNICODE)


def candidate_category(value, default='general'):
    """Map a chat memory candidate's category onto a stored memory category.

    A candidate the model explicitly marked preference, relationship or feedback
    keeps that category, so the memory it produces is visible to
    ``_collect_evidence``. Anything else stays ``general`` and never moves a
    personality value.
    """
    if type(value) is not str:
        return default
    category = value.strip().casefold()
    if category in ELIGIBLE_CATEGORIES:
        return category
    return default


# Keywords are compared against the case-folded, punctuation-free memory text.
TRAIT_KEYWORDS = {
    'warmth': ('warm', 'care', 'caring', 'comfort', 'hug', 'kind', 'together',
               '温暖', '关心', '抱', '喜欢', '亲近', '温柔', '贴心', '安慰', '陪'),
    'curiosity': ('curio', 'why', 'explor', 'learn', 'question',
                  '好奇', '为什么', '探索', '学习', '问题', '兴趣'),
    'playfulness': ('play', 'fun', 'joke', 'game', 'laugh',
                    '玩', '开心', '玩笑', '游戏', '逗', '有趣', '笑'),
    'directness': ('direct', 'straight', 'honest', 'clear',
                   '实话', '直接', '坦率', '明确', '直说', '老实'),
}


class GrowthError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _config(settings):
    merged = dict(DEFAULT_SETTINGS)
    if isinstance(settings, dict):
        merged.update({key: value for key, value in settings.items() if key in merged})
    return merged


def _normalize(value):
    if type(value) is not str:
        return ''
    return _NOISE.sub('', unicodedata.normalize('NFKC', value).casefold())


def _parse_time(value):
    if type(value) is not str or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _json_list(raw):
    if type(raw) is not str:
        return []
    try:
        value = json.loads(raw)
    except ValueError:
        return []
    if type(value) is not list:
        return []
    return [item for item in value if type(item) is str]


def _memory_rows(connection):
    """Read active memories, tolerating databases without memory extras."""
    columns = {row[1] for row in connection.execute('PRAGMA table_info(memories)')}
    required = {'id', 'content', 'category', 'source_type', 'created_at',
                'source_message_id'}
    if not required.issubset(columns):
        return []
    selected = ['id', 'content', 'category', 'source_type', 'created_at',
                'source_message_id']
    for name in ('dedupe_key', 'status', 'deleted_at'):
        if name in columns:
            selected.append(name)
    query = 'SELECT ' + ','.join(selected) + ' FROM memories WHERE subject_user_id=?'
    if 'status' in columns:
        query += " AND status='active'"
    if 'deleted_at' in columns:
        query += ' AND deleted_at IS NULL'
    query += ' ORDER BY created_at ASC, id ASC'
    cursor = connection.execute(query, (SUBJECT_USER_ID,))
    return [{description[0]: value for description, value in zip(cursor.description, row)}
            for row in cursor.fetchall()]


def _event_key(row):
    source = row.get('source_message_id')
    if type(source) is str and source.strip():
        return 'message:' + source.strip()
    stored = row.get('dedupe_key')
    if type(stored) is str and stored.strip():
        return 'dedupe:' + stored.strip()
    return 'text:' + _normalize(row.get('content'))


def _session_count(connection):
    row = connection.execute(
        'SELECT COUNT(*) FROM conversations WHERE subject_user_id=?',
        (SUBJECT_USER_ID,)).fetchone()
    return int(row[0]) if row else 0


def _collect_evidence(connection):
    """Map each growable trait to one memory id per distinct event."""
    evidence = {trait: {} for trait in character.GROWABLE_TRAITS}
    for row in _memory_rows(connection):
        category = row.get('category') or ''
        source_type = row.get('source_type') or ''
        if category not in ELIGIBLE_CATEGORIES and source_type not in ELIGIBLE_SOURCE_TYPES:
            continue
        text = _normalize(row.get('content'))
        if not text:
            continue
        for trait, keywords in TRAIT_KEYWORDS.items():
            if any(keyword in text for keyword in keywords):
                evidence[trait].setdefault(_event_key(row), row['id'])
    return evidence


def _has_pending(connection, trait):
    row = connection.execute(
        "SELECT 1 FROM growth_candidates WHERE subject_user_id=? AND trait=? "
        "AND status='pending' LIMIT 1", (SUBJECT_USER_ID, trait)).fetchone()
    return row is not None


def _decided_same_evidence(connection, trait, evidence_ids):
    """True when an applied or rejected attempt already used this evidence."""
    rows = connection.execute(
        "SELECT evidence_ids FROM growth_candidates WHERE subject_user_id=? AND trait=? "
        "AND status IN ('applied','rejected')", (SUBJECT_USER_ID, trait)).fetchall()
    target = set(evidence_ids)
    return any(set(_json_list(row[0])) == target for row in rows)


def _cooling_down(connection, trait, moment, cooldown_days):
    if cooldown_days <= 0:
        return False
    row = connection.execute(
        'SELECT MAX(created_at) FROM personality_history WHERE trait=?',
        (trait,)).fetchone()
    last = _parse_time(row[0]) if row else None
    if last is None:
        return False
    return (moment - last).total_seconds() < cooldown_days * 86400


def sync_candidates(connection, settings, now=None):
    """Insert new pending candidates and return the ones created this call."""
    config = _config(settings)
    if not config.get('enabled'):
        return []
    moment = character.normalize_now(now)
    if _session_count(connection) < int(config['min_sessions']):
        return []
    evidence = _collect_evidence(connection)
    locked = set(character.locked_traits(config))
    created = []
    for trait in character.GROWABLE_TRAITS:
        if trait in locked:
            continue
        events = evidence.get(trait, {})
        if len(events) < int(config['min_memories']):
            continue
        evidence_ids = list(events.values())
        if _has_pending(connection, trait):
            continue
        if _decided_same_evidence(connection, trait, evidence_ids):
            continue
        if _cooling_down(connection, trait, moment, int(config['cooldown_days'])):
            continue
        delta = round(min(float(config['max_step']), STEP_PER_EVENT * len(events)), 6)
        if delta <= 0:
            continue
        candidate_id = uuid.uuid4().hex
        stamp = moment.isoformat()
        # The content column keeps a human-readable explanation of the rule.
        content = f'{trait}: {len(events)} distinct events'
        connection.execute(
            'INSERT INTO growth_candidates(id,subject_user_id,category,content,status,'
            'created_at,updated_at,trait,delta,evidence_ids,rule_version,decision) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
            (candidate_id, SUBJECT_USER_ID, 'personality', content, 'pending',
             stamp, stamp, trait, delta,
             json.dumps(evidence_ids, ensure_ascii=False), RULE_VERSION, ''))
        created.append({'id': candidate_id, 'trait': trait, 'delta': delta,
                        'evidence_ids': evidence_ids, 'status': 'pending',
                        'created_at': stamp})
    return created


def list_candidates(connection, limit=CANDIDATE_LIMIT, status='pending'):
    """List candidates, pending by default so only actionable rows are shown."""
    if status is None:
        cursor = connection.execute(
            'SELECT id,trait,delta,evidence_ids,status,created_at FROM growth_candidates '
            'WHERE subject_user_id=? ORDER BY created_at DESC, id DESC LIMIT ?',
            (SUBJECT_USER_ID, limit))
    else:
        cursor = connection.execute(
            'SELECT id,trait,delta,evidence_ids,status,created_at FROM growth_candidates '
            'WHERE subject_user_id=? AND status=? '
            'ORDER BY created_at DESC, id DESC LIMIT ?',
            (SUBJECT_USER_ID, status, limit))
    return [{'id': row[0], 'trait': row[1], 'delta': float(row[2] or 0),
             'evidence_ids': _json_list(row[3]), 'status': row[4], 'created_at': row[5]}
            for row in cursor.fetchall()]


def history(connection, limit=HISTORY_LIMIT):
    cursor = connection.execute(
        'SELECT id,trait,old_value,new_value,created_at,reverted_at '
        'FROM personality_history ORDER BY created_at DESC, id DESC LIMIT ?',
        (limit,))
    return [{'id': row[0], 'trait': row[1], 'old_value': float(row[2]),
             'new_value': float(row[3]), 'created_at': row[4], 'reverted_at': row[5]}
            for row in cursor.fetchall()]


def _candidate(connection, candidate_id):
    return connection.execute(
        'SELECT id,trait,delta,evidence_ids,rule_version,status FROM growth_candidates '
        'WHERE id=? AND subject_user_id=?', (candidate_id, SUBJECT_USER_ID)).fetchone()


def approve(connection, candidate_id, settings, now=None):
    """Apply a pending candidate and record an undoable history row."""
    config = _config(settings)
    moment = character.normalize_now(now)
    row = _candidate(connection, candidate_id)
    if row is None:
        raise GrowthError('not_found', 'Growth candidate was not found.')
    if row[5] != 'pending':
        raise GrowthError('invalid_state', 'Only pending candidates can be approved.')
    trait = row[1]
    if not character.is_growable(trait) or trait in character.locked_traits(config):
        raise GrowthError('locked_trait', 'This trait is locked and cannot grow.')
    old_value, new_value = character.apply_trait_delta(
        connection, trait, float(row[2] or 0), float(config['max_step']), now=moment)
    history_id = uuid.uuid4().hex
    stamp = moment.isoformat()
    connection.execute(
        'INSERT INTO personality_history(id,trait,old_value,new_value,evidence_ids,'
        'rule_version,created_at) VALUES (?,?,?,?,?,?,?)',
        (history_id, trait, old_value, new_value, row[3] or '[]',
         int(row[4] or RULE_VERSION), stamp))
    connection.execute(
        "UPDATE growth_candidates SET status='applied', decision='approved', "
        'applied_at=?, updated_at=? WHERE id=?', (stamp, stamp, candidate_id))
    return {'status': 'applied', 'trait': trait, 'old_value': old_value,
            'new_value': new_value, 'history_id': history_id}


def reject(connection, candidate_id, now=None):
    moment = character.normalize_now(now)
    row = _candidate(connection, candidate_id)
    if row is None:
        raise GrowthError('not_found', 'Growth candidate was not found.')
    if row[5] != 'pending':
        raise GrowthError('invalid_state', 'Only pending candidates can be rejected.')
    connection.execute(
        "UPDATE growth_candidates SET status='rejected', decision='rejected', "
        'updated_at=? WHERE id=?', (moment.isoformat(), candidate_id))
    return {'status': 'rejected', 'candidate_id': candidate_id}


def rollback(connection, history_id, now=None):
    """Restore the old value recorded by one applied history row."""
    moment = character.normalize_now(now)
    row = connection.execute(
        'SELECT id,trait,old_value,reverted_at FROM personality_history WHERE id=?',
        (history_id,)).fetchone()
    if row is None:
        raise GrowthError('not_found', 'Personality history entry was not found.')
    if row[3]:
        raise GrowthError('invalid_state', 'This change has already been reverted.')
    trait = row[1]
    if not character.is_growable(trait):
        raise GrowthError('locked_trait', 'Core traits cannot be rolled back.')
    value = character.restore_trait(connection, trait, float(row[2]), now=moment)
    connection.execute(
        'UPDATE personality_history SET reverted_at=? WHERE id=?',
        (moment.isoformat(), history_id))
    return {'status': 'reverted', 'trait': trait, 'value': value}
