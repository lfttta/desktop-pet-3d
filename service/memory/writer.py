"""Memory write pipeline: propose, correct, delete and outbox recovery.

Every public function returns one of the five write outcomes defined in
OUTCOMES. Filesystem work for a correction happens before the metadata
transaction, so a failed body write leaves the original memory untouched.
Deletes are two-phase: a transaction marks the row ``deleting`` and enqueues an
outbox row, then process_outbox (called from the runtime tick) removes the
Markdown body and marks the row ``deleted``.
"""

import json
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

from service.database import SUBJECT_USER_ID
from service.memory import policy
from service.memory.store import (
    DEFAULT_CATEGORY, DEFAULT_CONFIDENCE, DEFAULT_IMPORTANCE, memory_summary,
    utc_now,
)

COMMITTED = 'committed'
MERGED = 'merged'
REJECTED = 'rejected'
QUEUED = 'queued'
SUPERSEDED = 'superseded'
ARCHIVED = 'archived'
ACTIVE = 'active'
OUTCOMES = (COMMITTED, MERGED, REJECTED, QUEUED, SUPERSEDED)

SOURCE_TYPES = frozenset({'manual', 'chat', 'correction', 'import', 'system', 'compaction'})
MAX_CATEGORY_CHARS = 64
MAX_TEXT_CHARS = 100000
DEDUPE_SCAN_LIMIT = 200
OUTBOX_BATCH_LIMIT = 50
# 发件箱重试上限与指数退避：永久失败的条目被标记 failed_at（保留可查），
# 不再每 100ms 重试，也不会饿死后面的健康条目。
OUTBOX_MAX_ATTEMPTS = 5
OUTBOX_BACKOFF_BASE_SECONDS = 30.0
OUTBOX_BACKOFF_MAX_SECONDS = 1800.0

_MEMORY_ID = re.compile(r'^[A-Za-z0-9_-]{1,128}$')

DEFAULT_SETTINGS = {
    'enabled': True,
    'auto_capture': False,
    'max_injected': 5,
    'max_entry_chars': 500,
}


def _settings(settings):
    merged = dict(DEFAULT_SETTINGS)
    if isinstance(settings, dict):
        merged.update({key: value for key, value in settings.items() if key in merged})
    return merged


def _clean_text(text, max_chars):
    if type(text) is not str:
        return None
    stripped = text.strip()
    if not stripped or len(stripped) > max_chars:
        return None
    try:
        stripped.encode('utf-8')
    except UnicodeError:
        return None
    return stripped


def _clean_category(category):
    if type(category) is not str:
        return None
    stripped = category.strip()
    if not stripped or len(stripped) > MAX_CATEGORY_CHARS:
        return None
    return stripped


def _clean_importance(importance):
    if type(importance) not in (int, float) or not 0.0 <= float(importance) <= 1.0:
        return None
    return float(importance)


def _clean_source_type(source_type):
    if type(source_type) is not str or source_type not in SOURCE_TYPES:
        return None
    return source_type


def _valid_memory_id(memory_id):
    return type(memory_id) is str and _MEMORY_ID.match(memory_id) is not None


def _summary(store, memory_id, subject_user_id=SUBJECT_USER_ID):
    return memory_summary(store.get_memory(memory_id, subject_user_id))


def _rejected(reason):
    return {'status': REJECTED, 'reason': reason, 'memory': None}


def _new_record(memory_id, text, category, importance, source_type, status,
                body_hash, now, subject_user_id, source_message_id=None,
                supersedes_id=None, fuzz_level=0, memory_level='short',
                topic_id=None, protected=0):
    return {
        'id': memory_id,
        'content': text,
        'created_at': now,
        'subject_user_id': subject_user_id,
        'category': category,
        'importance': importance,
        'source_type': source_type,
        'source_message_id': source_message_id,
        'updated_at': now,
        'deleted_at': None,
        'status': status,
        'body_hash': body_hash,
        'mention_count': 1,
        'session_count': 1,
        'recall_count': 0,
        'confidence': DEFAULT_CONFIDENCE,
        'strength': policy.strength(importance, 1, 1, 0, DEFAULT_CONFIDENCE),
        'last_reinforced_at': now,
        'dedupe_key': policy.normalize_text(text)[:200],
        'superseded_by': None,
        'supersedes_id': supersedes_id,
        'fuzz_level': int(fuzz_level or 0),
        'memory_level': memory_level or 'short',
        'topic_id': topic_id,
        'protected': 1 if protected else 0,
    }


def propose(store, text, category=DEFAULT_CATEGORY, importance=DEFAULT_IMPORTANCE,
            source_type='manual', settings=None, subject_user_id=SUBJECT_USER_ID,
            source_message_id=None):
    """Validate, dedupe/merge, then persist one explicit memory."""
    settings = _settings(settings)
    if not settings.get('enabled', True):
        return _rejected('memory_disabled')
    cleaned = _clean_text(text, int(settings.get('max_entry_chars', 500)))
    if cleaned is None:
        return _rejected('invalid_text')
    category = _clean_category(category)
    if category is None:
        return _rejected('invalid_category')
    importance = _clean_importance(importance)
    if importance is None:
        return _rejected('invalid_importance')
    source_type = _clean_source_type(source_type)
    if source_type is None:
        return _rejected('invalid_source_type')

    incoming_key = policy.normalize_text(cleaned)
    # 精确重复走 dedupe_key 索引；键被截断到 200 字符，命中后再做全文比对。
    for row in store.find_by_dedupe_key(incoming_key[:200], subject_user_id):
        if policy.normalize_text(row.get('content', '')) == incoming_key:
            return _merge(store, row, cleaned, importance, source_type, subject_user_id)
    # 短窗口内的主题合并只看最近活跃的行，不再全量扫描。
    cutoff = (datetime.now(timezone.utc)
              - timedelta(seconds=policy.DEDUPE_WINDOW_SECONDS)).strftime(
                  '%Y-%m-%dT%H:%M:%S.%fZ')
    for row in store.find_recent_active(cutoff, subject_user_id, limit=DEDUPE_SCAN_LIMIT):
        reference = (row.get('last_reinforced_at') or row.get('updated_at')
                     or row.get('created_at'))
        if policy.within_window(reference) and \
                policy.is_same_topic(row.get('content', ''), cleaned):
            return _merge(store, row, cleaned, importance, source_type, subject_user_id)

    memory_id = uuid.uuid4().hex
    now = utc_now()
    body_hash = ''
    try:
        body_hash = store.write_body(memory_id, cleaned)
    except OSError:
        body_hash = ''
    record = _new_record(memory_id, cleaned, category, importance, source_type,
                         'active' if body_hash else 'pending', body_hash, now,
                         subject_user_id, source_message_id)
    status = COMMITTED if body_hash else QUEUED
    try:
        with store.database.transaction() as connection:
            store.insert_memory(connection, record)
            store.record_operation(connection, memory_id, 'propose',
                                   {'status': status}, status=status)
            if not body_hash:
                store.enqueue(connection, memory_id, 'write_body', {'text': cleaned})
    except sqlite3.Error:
        if body_hash:
            store.delete_body(memory_id)
        raise
    if body_hash:
        store.index_memory(memory_id, cleaned)
        return {'status': COMMITTED, 'memory': _summary(store, memory_id, subject_user_id)}
    return {'status': QUEUED, 'reason': 'body_write_failed',
            'memory': _summary(store, memory_id, subject_user_id)}


def _merge(store, row, text, incoming_importance, source_type, subject_user_id):
    memory_id = row['id']
    mention_count = int(row.get('mention_count') or 1) + 1
    reference = (row.get('last_reinforced_at') or row.get('updated_at')
                 or row.get('created_at'))
    same_session = policy.within_window(reference)
    session_count = int(row.get('session_count') or 1) + (0 if same_session else 1)
    importance = policy.merge_importance(row.get('importance'), incoming_importance,
                                         mention_count - 1)
    confidence = policy.merge_confidence(row.get('confidence'), DEFAULT_CONFIDENCE,
                                         mention_count - 1)
    strength = policy.strength(importance, mention_count, session_count,
                               row.get('recall_count') or 0, confidence)
    now = utc_now()
    with store.database.transaction() as connection:
        store.update_memory(connection, memory_id, {
            'importance': importance,
            'confidence': confidence,
            'mention_count': mention_count,
            'session_count': session_count,
            'strength': strength,
            'last_reinforced_at': now,
            'updated_at': now,
        }, subject_user_id=subject_user_id)
        store.record_operation(connection, memory_id, 'merge', {
            'mention_count': mention_count,
            'importance': importance,
            'source_type': source_type,
        })
    return {'status': MERGED, 'memory': _summary(store, memory_id, subject_user_id)}


def _correction_record(target, memory_id, text, body_hash, now, subject_user_id):
    importance = max(float(target.get('importance') or DEFAULT_IMPORTANCE), 0.5)
    mention_count = max(1, int(target.get('mention_count') or 1))
    record = _new_record(memory_id, text, target.get('category') or DEFAULT_CATEGORY,
                         importance, 'correction', 'active', body_hash, now,
                         subject_user_id, supersedes_id=target.get('id'),
                         memory_level=target.get('memory_level') or 'short',
                         topic_id=target.get('topic_id'),
                         protected=target.get('protected'))
    record['mention_count'] = mention_count
    record['confidence'] = float(target.get('confidence') or DEFAULT_CONFIDENCE)
    record['strength'] = policy.strength(importance, mention_count, 1, 0,
                                         record['confidence'])
    return record


def correct(store, memory_id, text, settings=None, subject_user_id=SUBJECT_USER_ID):
    """Create a correction and supersede the old memory version."""
    if not _valid_memory_id(memory_id):
        return _rejected('invalid_memory_id')
    settings = _settings(settings)
    cleaned = _clean_text(text, int(settings.get('max_entry_chars', 500)))
    if cleaned is None:
        return _rejected('invalid_text')

    target = store.get_memory(memory_id, subject_user_id)
    if target is None or target.get('deleted_at') or target.get('status') != 'active':
        return _rejected('not_found')
    if policy.normalize_text(target.get('content', '')) == policy.normalize_text(cleaned):
        return _rejected('unchanged')

    new_id = uuid.uuid4().hex
    now = utc_now()
    try:
        body_hash = store.write_body(new_id, cleaned)
    except OSError:
        try:
            with store.database.transaction() as connection:
                store.enqueue(connection, memory_id, 'correct', {
                    'target_id': memory_id, 'new_id': new_id, 'text': cleaned})
        except sqlite3.Error:
            return _rejected('storage_error')
        return {'status': QUEUED, 'reason': 'body_write_failed', 'memory': None,
                'previous_memory_id': memory_id}

    record = _correction_record(target, new_id, cleaned, body_hash, now, subject_user_id)
    try:
        with store.database.transaction() as connection:
            store.insert_memory(connection, record)
            store.update_memory(connection, memory_id, {
                'status': 'superseded',
                'superseded_by': new_id,
                'updated_at': now,
            }, subject_user_id=subject_user_id)
            store.record_operation(connection, new_id, 'correct', {
                'previous_memory_id': memory_id})
            store.record_operation(connection, memory_id, 'supersede', {
                'superseded_by': new_id})
    except sqlite3.Error:
        store.delete_body(new_id)
        raise
    store.index_memory(new_id, cleaned)
    store.remove_index(memory_id)
    return {'status': SUPERSEDED, 'memory': _summary(store, new_id, subject_user_id),
            'previous_memory_id': memory_id}


def archive(store, memory_id, subject_user_id=SUBJECT_USER_ID):
    """Archive a memory and move its body into the archive directory."""
    if not _valid_memory_id(memory_id):
        return _rejected('invalid_memory_id')
    target = store.get_memory(memory_id, subject_user_id)
    if target is None or target.get('deleted_at'):
        return _rejected('not_found')
    status = target.get('status')
    if status == ARCHIVED:
        return {'status': ARCHIVED, 'memory_id': memory_id}
    if status not in ('active', 'superseded', 'disputed'):
        return _rejected('not_archivable')

    previous_fuzz = policy.clamp_fuzz_level(target.get('fuzz_level'))
    moved = False
    try:
        moved = store.archive_body(memory_id)
    except OSError:
        return _rejected('body_move_failed')

    now = utc_now()
    try:
        with store.database.transaction() as connection:
            store.update_memory(connection, memory_id, {
                'status': ARCHIVED,
                'fuzz_level': policy.FUZZ_ARCHIVED,
                'updated_at': now,
            }, subject_user_id=subject_user_id)
            store.record_operation(connection, memory_id, 'archive', {
                'previous_fuzz_level': previous_fuzz})
    except Exception:
        if moved:
            try:
                store.restore_body(memory_id)
            except OSError:
                pass
        raise
    store.remove_index(memory_id)
    return {'status': ARCHIVED, 'memory_id': memory_id}


def restore(store, memory_id, subject_user_id=SUBJECT_USER_ID):
    """Restore an archived memory and move its body back into the active store."""
    if not _valid_memory_id(memory_id):
        return _rejected('invalid_memory_id')
    target = store.get_memory(memory_id, subject_user_id)
    if target is None or target.get('deleted_at'):
        return _rejected('not_found')
    if target.get('status') != ARCHIVED:
        return _rejected('not_archived')

    payload = store.latest_operation_payload(memory_id, 'archive')
    previous_fuzz = policy.clamp_fuzz_level(payload.get('previous_fuzz_level', 0))
    moved = False
    try:
        moved = store.restore_body(memory_id)
    except OSError:
        return _rejected('body_move_failed')

    now = utc_now()
    try:
        with store.database.transaction() as connection:
            store.update_memory(connection, memory_id, {
                'status': 'active',
                'fuzz_level': previous_fuzz,
                'updated_at': now,
            }, subject_user_id=subject_user_id)
            store.record_operation(connection, memory_id, 'restore', {
                'fuzz_level': previous_fuzz})
    except Exception:
        if moved:
            try:
                store.archive_body(memory_id)
            except OSError:
                pass
        raise
    store.index_memory(memory_id, target.get('content', ''))
    return {'status': 'active', 'memory_id': memory_id}


def commit_compaction(store, memory_id, text, fuzz_level, settings=None,
                      subject_user_id=SUBJECT_USER_ID):
    """Commit one compaction version.

    The new body is written first, then the old body is moved to the archive
    directory, then metadata changes in one transaction. Any failure restores
    the old body and removes the new one, so the original never changes.
    """
    if not _valid_memory_id(memory_id):
        return _rejected('invalid_memory_id')
    settings = _settings(settings)
    cleaned = _clean_text(text, MAX_TEXT_CHARS)
    if cleaned is None:
        return _rejected('invalid_text')
    level = policy.clamp_fuzz_level(fuzz_level)
    if level <= policy.FUZZ_EXACT or level > policy.MAX_COMPACT_LEVEL:
        return _rejected('invalid_fuzz_level')

    target = store.get_memory(memory_id, subject_user_id)
    if target is None or target.get('deleted_at') or target.get('status') != 'active':
        return _rejected('not_active')
    if policy.normalize_text(target.get('content', '')) == policy.normalize_text(cleaned):
        return _rejected('unchanged')
    previous_fuzz = policy.clamp_fuzz_level(target.get('fuzz_level'))

    new_id = uuid.uuid4().hex
    now = utc_now()
    body_hash = store.write_body(new_id, cleaned)
    try:
        moved = store.archive_body(memory_id)
    except OSError:
        store.delete_body(new_id)
        raise

    mention_count = max(1, int(target.get('mention_count') or 1))
    record = _new_record(
        new_id, cleaned, target.get('category') or DEFAULT_CATEGORY,
        float(target.get('importance') or DEFAULT_IMPORTANCE), 'compaction',
        'active', body_hash, now, subject_user_id,
        supersedes_id=memory_id, fuzz_level=level,
        memory_level=target.get('memory_level') or 'short',
        topic_id=target.get('topic_id'), protected=target.get('protected'))
    record['mention_count'] = mention_count
    record['confidence'] = float(target.get('confidence') or DEFAULT_CONFIDENCE)
    record['strength'] = policy.strength(record['importance'], mention_count, 1, 0,
                                         record['confidence'])

    try:
        with store.database.transaction() as connection:
            store.insert_memory(connection, record)
            store.update_memory(connection, memory_id, {
                'status': 'superseded',
                'superseded_by': new_id,
                'fuzz_level': policy.next_fuzz_level(previous_fuzz),
                'updated_at': now,
            }, subject_user_id=subject_user_id)
            store.record_operation(connection, new_id, 'compact', {
                'previous_memory_id': memory_id, 'fuzz_level': level})
            store.record_operation(connection, memory_id, 'supersede', {
                'superseded_by': new_id, 'reason': 'compaction', 'fuzz_level': level})
    except Exception:
        store.delete_body(new_id)
        if moved:
            try:
                store.restore_body(memory_id)
            except OSError:
                pass
        raise

    store.index_memory(new_id, cleaned)
    store.remove_index(memory_id)
    return {'status': SUPERSEDED, 'memory': _summary(store, new_id, subject_user_id),
            'previous_memory_id': memory_id, 'fuzz_level': level}


def delete(store, memory_id, subject_user_id=SUBJECT_USER_ID):
    """Mark a memory deleting and enqueue body removal for the tick."""
    if not _valid_memory_id(memory_id):
        return _rejected('invalid_memory_id')
    with store.database.transaction() as connection:
        target = store.get_memory(memory_id, subject_user_id)
        if target is None or target.get('deleted_at'):
            return _rejected('not_found')
        status = target.get('status')
        if status == 'deleting':
            return {'status': QUEUED, 'reason': 'already_deleting', 'memory_id': memory_id}
        if status not in ('active', 'superseded'):
            return _rejected('not_found')
        now = utc_now()
        store.update_memory(connection, memory_id, {
            'status': 'deleting',
            'deleted_at': now,
            'updated_at': now,
        }, subject_user_id=subject_user_id)
        store.record_operation(connection, memory_id, 'delete', {}, status='pending')
        store.enqueue(connection, memory_id, 'delete_body', {})
    return {'status': QUEUED, 'memory_id': memory_id}


# -- outbox processing ---------------------------------------------------


def _complete_delete(store, entry):
    memory_id = entry.get('memory_id') or ''
    if not _valid_memory_id(memory_id):
        return 'failed'
    store.delete_body(memory_id)
    store.remove_index(memory_id)
    now = utc_now()
    with store.database.transaction() as connection:
        store.update_memory(connection, memory_id, {
            'status': 'deleted', 'updated_at': now})
        store.complete_pending_operation(connection, memory_id, 'delete')
        store.mark_outbox_delivered(connection, entry['id'])
    return 'deleted'


def _complete_write_body(store, entry, payload):
    memory_id = entry.get('memory_id') or ''
    text = payload.get('text')
    if not _valid_memory_id(memory_id) or _clean_text(text, MAX_TEXT_CHARS) is None:
        return 'failed'
    body_hash = store.write_body(memory_id, text)
    now = utc_now()
    with store.database.transaction() as connection:
        activated = store.activate_pending_memory(connection, memory_id, body_hash, now)
        store.complete_pending_operation(connection, memory_id, 'propose')
        store.mark_outbox_delivered(connection, entry['id'])
    if activated:
        store.index_memory(memory_id, text)
    return COMMITTED if activated else 'skipped'


def _complete_correction(store, entry, payload):
    target_id = payload.get('target_id')
    new_id = payload.get('new_id')
    text = payload.get('text')
    if not _valid_memory_id(target_id) or not _valid_memory_id(new_id):
        return 'failed'
    if _clean_text(text, MAX_TEXT_CHARS) is None:
        return 'failed'
    target = store.get_memory(target_id)
    if target is None or target.get('deleted_at') or target.get('status') != 'active':
        with store.database.transaction() as connection:
            store.mark_outbox_delivered(connection, entry['id'])
        return 'skipped'
    body_hash = store.write_body(new_id, text)
    now = utc_now()
    record = _correction_record(target, new_id, text, body_hash, now, SUBJECT_USER_ID)
    with store.database.transaction() as connection:
        store.insert_memory(connection, record, or_ignore=True)
        store.update_memory(connection, target_id, {
            'status': 'superseded',
            'superseded_by': new_id,
            'updated_at': now,
        })
        store.record_operation(connection, new_id, 'correct', {
            'previous_memory_id': target_id, 'recovered': True})
        store.mark_outbox_delivered(connection, entry['id'])
    store.index_memory(new_id, text)
    store.remove_index(target_id)
    return SUPERSEDED


def _outbox_backoff(attempts):
    """第 attempts 次失败后的退避秒数（指数增长，封顶）。"""
    delay = OUTBOX_BACKOFF_BASE_SECONDS * (2 ** max(0, attempts - 1))
    return min(delay, OUTBOX_BACKOFF_MAX_SECONDS)


def process_outbox(store, limit=OUTBOX_BATCH_LIMIT):
    """Drain pending outbox rows; safe to call repeatedly from the tick.

    失败条目按指数退避重试；达到上限后标记 ``failed_at`` 成为死信，
    保留在表里可供检查，不再参与选取，也不会阻塞健康条目。
    """
    processed = []
    now = utc_now()
    for entry in store.pending_outbox(limit, now=now):
        operation = entry.get('operation')
        try:
            payload = json.loads(entry.get('payload') or '{}')
        except ValueError:
            payload = {}
        try:
            if operation == 'delete_body':
                outcome = _complete_delete(store, entry)
            elif operation == 'write_body':
                outcome = _complete_write_body(store, entry, payload)
            elif operation == 'correct':
                outcome = _complete_correction(store, entry, payload)
            else:
                outcome = 'failed'
        except (OSError, sqlite3.Error):
            outcome = 'failed'
        if outcome == 'failed':
            attempts = int(entry.get('attempts') or 0) + 1
            next_attempt_at = None
            failed_at = None
            if attempts >= OUTBOX_MAX_ATTEMPTS:
                failed_at = now
            else:
                retry_at = datetime.now(timezone.utc) + timedelta(
                    seconds=_outbox_backoff(attempts))
                next_attempt_at = retry_at.strftime('%Y-%m-%dT%H:%M:%S.%fZ')
            try:
                with store.database.transaction() as connection:
                    store.bump_outbox_attempt(connection, entry['id'],
                                              next_attempt_at=next_attempt_at,
                                              failed_at=failed_at)
            except sqlite3.Error:
                pass
            if failed_at is not None:
                outcome = 'dead'
        processed.append({'outbox_id': entry['id'], 'operation': operation,
                          'status': outcome})
    return processed
