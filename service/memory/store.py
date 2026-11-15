"""SQLite metadata plus Markdown bodies for the minimal memory store.

The service owns one SQLite connection; every method here uses it and never
opens a second connection. Markdown bodies live under
``<data-dir>/memory/bodies/<id>.md`` and are written through a temporary file
followed by an atomic replace, so a failed write never damages an old body.
"""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import uuid

from service.database import SUBJECT_USER_ID

BODY_SUBDIRECTORY = 'memory'
BODY_DIRECTORY = 'bodies'
ARCHIVE_DIRECTORY = 'archive'
DEFAULT_CATEGORY = 'general'
DEFAULT_IMPORTANCE = 0.5
DEFAULT_CONFIDENCE = 0.5
SUMMARY_SNIPPET_CHARS = 160
MAX_CHAIN_LENGTH = 100
# Recall is capped so a frequently injected memory cannot build an unbounded
# ranking advantage over newer memories.
MAX_RECALL_COUNT = 50

# Columns owned by this module. database.py creates the base table; the extra
# columns are added here so the memory feature stays self-contained.
EXTRA_COLUMNS = (
    ('status', "TEXT NOT NULL DEFAULT 'active'"),
    ('body_hash', "TEXT NOT NULL DEFAULT ''"),
    ('mention_count', 'INTEGER NOT NULL DEFAULT 1'),
    ('session_count', 'INTEGER NOT NULL DEFAULT 1'),
    ('recall_count', 'INTEGER NOT NULL DEFAULT 0'),
    ('confidence', 'REAL NOT NULL DEFAULT 0.5'),
    ('strength', 'REAL NOT NULL DEFAULT 0.0'),
    ('last_reinforced_at', "TEXT NOT NULL DEFAULT ''"),
    ('dedupe_key', "TEXT NOT NULL DEFAULT ''"),
    ('superseded_by', 'TEXT'),
    ('supersedes_id', 'TEXT'),
    ('fuzz_level', 'INTEGER DEFAULT 0'),
    ('memory_level', "TEXT DEFAULT 'short'"),
    ('topic_id', 'TEXT'),
    ('protected', 'INTEGER DEFAULT 0'),
    # 事实发生时间（双时态最小子集，只取 valid 侧）：可空。写入时从原文
    # 确定性解析，解析不出就存 NULL（fail-open），读取侧回退 updated_at，
    # 与旧行为等价。
    ('occurred_at', 'TEXT'),
)

MEMORY_COLUMNS = (
    'id', 'content', 'created_at', 'subject_user_id', 'category', 'importance',
    'source_type', 'source_message_id', 'updated_at', 'deleted_at', 'status',
    'body_hash', 'mention_count', 'session_count', 'recall_count', 'confidence',
    'strength', 'last_reinforced_at', 'dedupe_key', 'superseded_by',
    'supersedes_id', 'fuzz_level', 'memory_level', 'topic_id', 'protected',
    'occurred_at',
)

WRITABLE_COLUMNS = frozenset(MEMORY_COLUMNS) - {'id'}


def utc_now():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')


def markdown_document(text):
    return '# Memory\n\n' + text.strip() + '\n'


def _row_dict(cursor, row):
    if row is None:
        return None
    return {description[0]: value for description, value in zip(cursor.description, row)}


def _rows(cursor):
    return [_row_dict(cursor, row) for row in cursor.fetchall()]


def _snippet(content, limit=SUMMARY_SNIPPET_CHARS):
    text = (content or '').strip()
    return text if len(text) <= limit else text[:limit].rstrip() + '…'


def memory_summary(row):
    if row is None:
        return None
    return {
        'id': row.get('id'),
        'text': row.get('content', ''),
        'snippet': _snippet(row.get('content', '')),
        'category': row.get('category', DEFAULT_CATEGORY),
        'importance': float(row.get('importance') or 0.0),
        'strength': float(row.get('strength') or 0.0),
        'mention_count': int(row.get('mention_count') or 0),
        'recall_count': int(row.get('recall_count') or 0),
        'status': row.get('status', 'active'),
        'created_at': row.get('created_at'),
        'updated_at': row.get('updated_at'),
        'occurred_at': row.get('occurred_at'),
        'last_reinforced_at': row.get('last_reinforced_at'),
        'fuzz_level': int(row.get('fuzz_level') or 0),
        'memory_level': row.get('memory_level') or 'short',
        'topic_id': row.get('topic_id'),
        'protected': bool(row.get('protected')),
        'supersedes_id': row.get('supersedes_id'),
        'superseded_by': row.get('superseded_by'),
    }


class MemoryStore:
    def __init__(self, database, data_dir=None):
        self.database = database
        self.connection = database.connection
        self.path = Path(database.path)
        self.data_dir = Path(data_dir) if data_dir is not None else self.path.parent
        self.bodies_dir = self.data_dir / BODY_SUBDIRECTORY / BODY_DIRECTORY
        self.archive_dir = self.data_dir / BODY_SUBDIRECTORY / ARCHIVE_DIRECTORY
        self.fts_enabled = bool(getattr(database, 'fts_enabled', False))
        self._ensure_schema()

    # -- schema ---------------------------------------------------------

    def _ensure_schema(self):
        with self.database.transaction() as connection:
            existing = {row[1] for row in connection.execute('PRAGMA table_info(memories)')}
            for name, declaration in EXTRA_COLUMNS:
                if name not in existing:
                    connection.execute(f'ALTER TABLE memories ADD COLUMN {name} {declaration}')
            connection.execute('CREATE INDEX IF NOT EXISTS idx_memories_status '
                               'ON memories(subject_user_id, status, updated_at)')
            connection.execute('CREATE INDEX IF NOT EXISTS idx_memories_dedupe '
                               'ON memories(subject_user_id, dedupe_key)')
            self._backfill_dedupe_keys(connection)

    @staticmethod
    def _backfill_dedupe_keys(connection):
        """为历史行补算 dedupe_key，让精确去重可以走索引。"""
        from service.memory import policy
        rows = connection.execute(
            "SELECT id, content FROM memories WHERE dedupe_key='' AND content<>''"
        ).fetchall()
        if rows:
            connection.executemany(
                'UPDATE memories SET dedupe_key=? WHERE id=?',
                [(policy.normalize_text(content)[:200], memory_id)
                 for memory_id, content in rows])

    # -- metadata -------------------------------------------------------

    def get_memory(self, memory_id, subject_user_id=SUBJECT_USER_ID):
        cursor = self.connection.execute(
            'SELECT * FROM memories WHERE id=? AND subject_user_id=?',
            (memory_id, subject_user_id))
        return _row_dict(cursor, cursor.fetchone())

    def find_active(self, subject_user_id=SUBJECT_USER_ID, limit=200):
        cursor = self.connection.execute(
            "SELECT * FROM memories WHERE subject_user_id=? AND status='active' "
            "AND deleted_at IS NULL ORDER BY updated_at DESC, created_at DESC LIMIT ?",
            (subject_user_id, limit))
        return _rows(cursor)

    def iter_active(self, subject_user_id=SUBJECT_USER_ID, batch_size=200):
        """按 (updated_at, created_at, id) 键集分页遍历全部活跃记忆。

        维护任务不再被 find_active 的 200 条上限截断；排序稳定且分页
        不依赖 OFFSET，行数增长时代价保持线性。
        """
        batch_size = max(1, int(batch_size))
        last = None
        while True:
            if last is None:
                cursor = self.connection.execute(
                    "SELECT * FROM memories WHERE subject_user_id=? AND status='active' "
                    "AND deleted_at IS NULL ORDER BY updated_at, created_at, id LIMIT ?",
                    (subject_user_id, batch_size))
            else:
                cursor = self.connection.execute(
                    "SELECT * FROM memories WHERE subject_user_id=? AND status='active' "
                    "AND deleted_at IS NULL AND (updated_at, created_at, id) > (?,?,?) "
                    "ORDER BY updated_at, created_at, id LIMIT ?",
                    (subject_user_id, *last, batch_size))
            rows = _rows(cursor)
            if not rows:
                return
            yield from rows
            tail = rows[-1]
            last = (tail.get('updated_at') or '', tail.get('created_at') or '',
                    tail['id'])

    def find_by_dedupe_key(self, dedupe_key, subject_user_id=SUBJECT_USER_ID):
        """走 idx_memories_dedupe 索引的精确去重候选。"""
        cursor = self.connection.execute(
            "SELECT * FROM memories WHERE subject_user_id=? AND dedupe_key=? "
            "AND status='active' AND deleted_at IS NULL",
            (subject_user_id, dedupe_key))
        return _rows(cursor)

    def duplicate_dedupe_keys(self, subject_user_id=SUBJECT_USER_ID):
        """一条 GROUP BY 查询找出存在重复的 dedupe_key，替代两两比较。"""
        cursor = self.connection.execute(
            "SELECT dedupe_key FROM memories WHERE subject_user_id=? "
            "AND status='active' AND deleted_at IS NULL AND dedupe_key<>'' "
            'GROUP BY dedupe_key HAVING COUNT(*) > 1', (subject_user_id,))
        return [row[0] for row in cursor.fetchall()]

    def find_recent_active(self, since, subject_user_id=SUBJECT_USER_ID, limit=200):
        """短窗口内可能参与主题合并的活跃记忆（调用方再做精确判断）。"""
        cursor = self.connection.execute(
            "SELECT * FROM memories WHERE subject_user_id=? AND status='active' "
            'AND deleted_at IS NULL AND (last_reinforced_at >= ? OR updated_at >= ? '
            'OR created_at >= ?) ORDER BY updated_at DESC, created_at DESC LIMIT ?',
            (subject_user_id, since, since, since, limit))
        return _rows(cursor)

    def list_memories(self, subject_user_id=SUBJECT_USER_ID, limit=20, category=None):
        """Return every non-deleted memory, including archived and disputed rows.

        Hiding inactive statuses here made archived memories unreachable from the
        UI and made memory.restore impossible once a row left ``active``.
        """
        if category:
            cursor = self.connection.execute(
                "SELECT * FROM memories WHERE subject_user_id=? "
                "AND deleted_at IS NULL AND category=? "
                "ORDER BY updated_at DESC, created_at DESC LIMIT ?",
                (subject_user_id, category, limit))
        else:
            cursor = self.connection.execute(
                "SELECT * FROM memories WHERE subject_user_id=? "
                "AND deleted_at IS NULL ORDER BY updated_at DESC, created_at DESC LIMIT ?",
                (subject_user_id, limit))
        return _rows(cursor)

    def version_chain(self, memory_id, subject_user_id=SUBJECT_USER_ID):
        """Return every version linked to memory_id, oldest first."""
        row = self.get_memory(memory_id, subject_user_id)
        if row is None:
            return []
        chain = [row]
        seen = {row['id']}

        cursor = row
        for _ in range(MAX_CHAIN_LENGTH):
            previous_id = cursor.get('supersedes_id')
            if not previous_id or previous_id in seen:
                break
            previous = self.get_memory(previous_id, subject_user_id)
            if previous is None:
                break
            chain.insert(0, previous)
            seen.add(previous_id)
            cursor = previous

        cursor = row
        for _ in range(MAX_CHAIN_LENGTH):
            next_id = cursor.get('superseded_by')
            if not next_id or next_id in seen:
                break
            following = self.get_memory(next_id, subject_user_id)
            if following is None:
                break
            chain.append(following)
            seen.add(next_id)
            cursor = following

        return chain

    def history_versions(self, memory_id, subject_user_id=SUBJECT_USER_ID):
        """Version descriptors in the protocol ``memory.history`` shape."""
        return [{
            'id': row.get('id'),
            'text': row.get('content', ''),
            'status': row.get('status', 'active'),
            'created_at': row.get('created_at'),
            'source_type': row.get('source_type', 'manual'),
        } for row in self.version_chain(memory_id, subject_user_id)]

    # -- topics ---------------------------------------------------------

    def topic_members(self, topic_id, subject_user_id=SUBJECT_USER_ID):
        cursor = self.connection.execute(
            'SELECT * FROM memories WHERE subject_user_id=? AND topic_id=? '
            'AND deleted_at IS NULL ORDER BY updated_at, created_at',
            (subject_user_id, topic_id))
        return _rows(cursor)

    @staticmethod
    def topic_key_for(row):
        from service.memory import policy
        return row.get('topic_id') or policy.topic_key(row.get('content', ''))

    @classmethod
    def group_by_topic(cls, rows):
        groups = {}
        for row in rows:
            groups.setdefault(cls.topic_key_for(row), []).append(row)
        return groups

    def insert_memory(self, connection, record, or_ignore=False):
        columns = [name for name in MEMORY_COLUMNS if name in record]
        if 'id' not in columns or 'content' not in columns:
            raise ValueError('memory records require id and content')
        values = [record[name] for name in columns]
        if 'occurred_at' not in columns:
            # 写入时回填事实发生时间。解析不出就存 NULL：读取侧回退
            # updated_at，recency 仍随「最后提起」衰减，与旧行为等价。
            # 若在此处落 created_at，occurred_at 读取优先会让无时间 token
            # 的行在再次提起后按「首次写入」衰减，偏离旧行为。
            # 延迟导入避免 store↔policy 环（同 record_recall 先例）。
            from service.memory import policy
            values.append(policy.occurred_at_for(
                record.get('content'), record.get('created_at')))
            columns.append('occurred_at')
        placeholders = ','.join('?' for _ in columns)
        verb = 'INSERT OR IGNORE' if or_ignore else 'INSERT'
        connection.execute(
            f'{verb} INTO memories({",".join(columns)}) VALUES ({placeholders})',
            values)

    def update_memory(self, connection, memory_id, fields, subject_user_id=SUBJECT_USER_ID):
        updates = {name: value for name, value in fields.items() if name in WRITABLE_COLUMNS}
        if not updates:
            return 0
        assignments = ','.join(f'{name}=?' for name in updates)
        cursor = connection.execute(
            f'UPDATE memories SET {assignments} WHERE id=? AND subject_user_id=?',
            [*updates.values(), memory_id, subject_user_id])
        return cursor.rowcount

    def activate_pending_memory(self, connection, memory_id, body_hash, updated_at):
        cursor = connection.execute(
            "UPDATE memories SET status='active', body_hash=?, updated_at=? "
            "WHERE id=? AND status='pending' AND deleted_at IS NULL",
            (body_hash, updated_at, memory_id))
        return cursor.rowcount

    def record_recall(self, memory_ids, subject_user_id=SUBJECT_USER_ID):
        """Increment recall counters and recompute strength in one transaction.

        Retrieval calls this after choosing which memories to inject. A storage
        failure must never fail the search, so every error returns 0 and leaves
        the previous counters untouched.
        """
        ids = []
        seen = set()
        for memory_id in memory_ids or ():
            if type(memory_id) is not str or not memory_id or memory_id in seen:
                continue
            seen.add(memory_id)
            ids.append(memory_id)
        if not ids:
            return 0
        from service.memory import policy
        try:
            with self.database.transaction() as connection:
                placeholders = ','.join('?' for _ in ids)
                rows = connection.execute(
                    'SELECT id, importance, mention_count, session_count, '
                    'recall_count, confidence FROM memories '
                    f'WHERE subject_user_id=? AND id IN ({placeholders})',
                    [subject_user_id, *ids]).fetchall()
                for row in rows:
                    recall_count = min(MAX_RECALL_COUNT, int(row[4] or 0) + 1)
                    strength = policy.strength(
                        row[1], row[2] or 1, row[3] or 1, recall_count, row[5])
                    connection.execute(
                        'UPDATE memories SET recall_count=?, strength=? '
                        'WHERE id=? AND subject_user_id=?',
                        (recall_count, strength, row[0], subject_user_id))
            return len(rows)
        except (sqlite3.Error, TypeError, ValueError):
            return 0

    def record_operation(self, connection, memory_id, operation, payload=None, status='applied'):
        connection.execute(
            'INSERT INTO memory_operations(id,memory_id,operation,payload,status) '
            'VALUES (?,?,?,?,?)',
            (uuid.uuid4().hex, memory_id, operation,
             json.dumps(payload or {}, ensure_ascii=False), status))

    def complete_pending_operation(self, connection, memory_id, operation):
        connection.execute(
            "UPDATE memory_operations SET status='applied' "
            "WHERE memory_id=? AND operation=? AND status='pending'",
            (memory_id, operation))

    def latest_operation_payload(self, memory_id, operation):
        cursor = self.connection.execute(
            'SELECT payload FROM memory_operations WHERE memory_id=? AND operation=? '
            'ORDER BY created_at DESC, rowid DESC LIMIT 1',
            (memory_id, operation))
        row = cursor.fetchone()
        if row is None:
            return {}
        try:
            value = json.loads(row[0] or '{}')
        except ValueError:
            return {}
        return value if isinstance(value, dict) else {}

    def enqueue(self, connection, memory_id, operation, payload=None):
        cursor = connection.execute(
            "INSERT INTO memory_outbox(memory_id,operation,payload) VALUES (?,?,?)",
            (memory_id, operation, json.dumps(payload or {}, ensure_ascii=False)))
        return cursor.lastrowid

    def pending_outbox(self, limit=50, now=None):
        """待处理发件箱行：跳过已失败（毒丸）与仍在退避窗口内的条目。"""
        stamp = now or utc_now()
        cursor = self.connection.execute(
            'SELECT * FROM memory_outbox WHERE delivered_at IS NULL '
            'AND failed_at IS NULL AND (next_attempt_at IS NULL OR next_attempt_at <= ?) '
            'ORDER BY id LIMIT ?', (stamp, limit))
        return _rows(cursor)

    def mark_outbox_delivered(self, connection, outbox_id):
        connection.execute(
            'UPDATE memory_outbox SET delivered_at=? WHERE id=? AND delivered_at IS NULL',
            (utc_now(), outbox_id))

    def bump_outbox_attempt(self, connection, outbox_id, next_attempt_at=None,
                            failed_at=None):
        """失败一次：计数加一，并安排退避重试或标记为永久失败。"""
        connection.execute(
            'UPDATE memory_outbox SET attempts=attempts+1, next_attempt_at=?, '
            'failed_at=? WHERE id=?', (next_attempt_at, failed_at, outbox_id))

    def outbox_counts(self):
        """发件箱可观测计数：待处理（含退避中）与永久失败。"""
        pending = 0
        failed = 0
        for delivered, failed_at, count in self.connection.execute(
                'SELECT delivered_at, failed_at, COUNT(*) FROM memory_outbox '
                'GROUP BY delivered_at IS NULL, failed_at IS NULL'):
            if delivered is not None:
                continue
            if failed_at is not None:
                failed += int(count)
            else:
                pending += int(count)
        return {'pending': pending, 'failed': failed}

    # -- Markdown bodies -------------------------------------------------

    def body_path(self, memory_id):
        return self.bodies_dir / f'{memory_id}.md'

    def archive_path(self, memory_id):
        return self.archive_dir / f'{memory_id}.md'

    def body_exists(self, memory_id):
        return self.body_path(memory_id).is_file()

    def archive_body(self, memory_id):
        """Move the active body into the archive directory.

        ``os.replace`` is atomic on the same volume; when it fails the source
        file is still present, so the caller can leave metadata untouched.
        """
        source = self.body_path(memory_id)
        if not source.is_file():
            return False
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        os.replace(source, self.archive_path(memory_id))
        return True

    def restore_body(self, memory_id):
        """Move an archived body back into the active bodies directory."""
        source = self.archive_path(memory_id)
        if not source.is_file():
            return False
        self.bodies_dir.mkdir(parents=True, exist_ok=True)
        os.replace(source, self.body_path(memory_id))
        return True

    def write_body(self, memory_id, text):
        self.bodies_dir.mkdir(parents=True, exist_ok=True)
        payload = markdown_document(text).encode('utf-8')
        digest = hashlib.sha256(payload).hexdigest()
        descriptor, name = tempfile.mkstemp(prefix=f'.{memory_id}-', suffix='.tmp',
                                            dir=self.bodies_dir)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.body_path(memory_id))
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                # A failed cleanup must not mask the original write failure.
                pass
        return digest

    def read_body(self, memory_id):
        for path in (self.body_path(memory_id), self.archive_path(memory_id)):
            try:
                return path.read_text('utf-8')
            except FileNotFoundError:
                continue
        return None

    def delete_body(self, memory_id):
        self.body_path(memory_id).unlink(missing_ok=True)
        self.archive_path(memory_id).unlink(missing_ok=True)

    # -- full-text index -------------------------------------------------

    def _write_index(self, connection, memory_id, content):
        from service.memory.search import index_text
        connection.execute('DELETE FROM memory_fts WHERE memory_id=?', (memory_id,))
        connection.execute('INSERT INTO memory_fts(memory_id,content) VALUES (?,?)',
                           (memory_id, index_text(content)))

    def index_memory(self, memory_id, content):
        if not self.fts_enabled:
            return False
        try:
            with self.database.transaction() as connection:
                self._write_index(connection, memory_id, content)
            return True
        except sqlite3.Error:
            # A damaged index must not fail the memory write; rebuild recovers.
            return False

    def remove_index(self, memory_id):
        if not self.fts_enabled:
            return False
        try:
            with self.database.transaction() as connection:
                connection.execute('DELETE FROM memory_fts WHERE memory_id=?', (memory_id,))
            return True
        except sqlite3.Error:
            return False

    def fts_candidates(self, subject_user_id, expression, limit):
        """按 FTS5 bm25 相关性升序返回候选（数值越小越相关）。

        bm25 自带 IDF，常见虚词不会与关键实词同权；候选上限因此只影响
        召回余量而不影响排序质量（LIMIT 在排序之后生效）。附带
        ``fts_rank`` 列供上层在 Python 侧保持这一相关性次序。
        """
        cursor = self.connection.execute(
            "SELECT m.*, bm25(memory_fts) AS fts_rank FROM memory_fts "
            "JOIN memories AS m ON m.id = memory_fts.memory_id "
            "WHERE memory_fts MATCH ? AND m.subject_user_id=? "
            "AND m.status='active' AND m.deleted_at IS NULL "
            "ORDER BY fts_rank LIMIT ?",
            (expression, subject_user_id, limit))
        return _rows(cursor)

    @staticmethod
    def _like_pattern(term):
        escaped = term.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
        return '%' + escaped + '%'

    def like_candidates(self, subject_user_id, terms, limit):
        clauses = ' OR '.join("content LIKE ? ESCAPE '\\'" for _ in terms)
        parameters = [self._like_pattern(term) for term in terms]
        cursor = self.connection.execute(
            "SELECT * FROM memories WHERE subject_user_id=? AND status='active' "
            "AND deleted_at IS NULL AND (" + clauses + ") LIMIT ?",
            [subject_user_id, *parameters, limit])
        return _rows(cursor)

    def search_candidates(self, query, terms, limit, subject_user_id=SUBJECT_USER_ID):
        from service.memory.search import match_expression
        if self.fts_enabled and terms:
            expression = match_expression(query)
            if expression:
                try:
                    rows = self.fts_candidates(subject_user_id, expression, limit)
                    if rows:
                        return rows
                except sqlite3.Error:
                    # Missing or corrupt index: fall back to LIKE below.
                    pass
        if not terms:
            return []
        return self.like_candidates(subject_user_id, terms, limit)

    def rebuild_index(self):
        """Rebuild the FTS table from the memories table; return rows indexed."""
        from service.memory.search import index_text
        try:
            with self.database.transaction() as connection:
                connection.execute(
                    'CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts '
                    "USING fts5(memory_id UNINDEXED, content, tokenize='unicode61')")
                rows = connection.execute(
                    "SELECT id, content FROM memories WHERE status='active' "
                    "AND deleted_at IS NULL").fetchall()
                connection.execute('DELETE FROM memory_fts')
                connection.executemany(
                    'INSERT INTO memory_fts(memory_id,content) VALUES (?,?)',
                    [(row[0], index_text(row[1])) for row in rows])
            self.fts_enabled = True
            return len(rows)
        except sqlite3.Error:
            return 0
