"""Redacted diagnostic bundle export.

The bundle contains an export manifest, a settings snapshot without credentials
or secrets, operational counts and the bounded event-log tail. Conversation and
memory bodies, prompts and provider keys are never read into the archive. The
archive is written below ``<data-dir>/exports`` and capped at 2 MB.
"""

from datetime import datetime, timezone
import hashlib
import io
import os
from pathlib import Path
import platform
import re
import sqlite3
import zipfile

from service import PROTOCOL_VERSION, SERVICE_VERSION
from service.jsonutil import dumps
from service.protocol import ProtocolError

NAME = 'diag'
CAPABILITY = {
    'available': True,
    'reason': '',
    'commands': ['diag.export'],
}

MAX_BUNDLE_BYTES = 2 * 1024 * 1024
LOG_TAIL = 200
EXPORT_DIRNAME = 'exports'
BUNDLE_FORMAT_VERSION = 1
REDACTED = '[redacted]'

_SENSITIVE_KEY = re.compile(
    r'(^|_)(api[_-]?key|key|secret|password|credential|token|authorization)$',
    re.IGNORECASE)
_KEY_TEXT = re.compile(r'(sk-[A-Za-z0-9_-]{8,}|Bearer\s+[A-Za-z0-9._-]{8,})')


def _utc_stamp():
    return datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')


def _redact_text(value):
    return _KEY_TEXT.sub(REDACTED, value)


def _redact(value):
    """Return a copy with credential-shaped keys and values replaced."""
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if _SENSITIVE_KEY.match(str(key)):
                result[str(key)] = REDACTED
            else:
                result[str(key)] = _redact(item)
        return result
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    return value


def _data_directory(store):
    path = getattr(store, 'path', None)
    if path is None:
        return None
    return Path(path).resolve().parent


def _inside_data_dir(data_dir, candidate):
    """True only when ``candidate`` resolves to ``data_dir`` or below it."""
    try:
        directory = Path(data_dir).resolve()
        target = Path(candidate).resolve()
    except (OSError, RuntimeError):
        return False
    return target == directory or directory in target.parents


def _count(connection, sql):
    try:
        row = connection.execute(sql).fetchone()
    except sqlite3.Error:
        return 0
    return int(row[0]) if row and isinstance(row[0], int) else 0


def _diagnostics(store, ctx):
    database = getattr(ctx, 'database', None)
    connection = getattr(database, 'connection', None)
    counts = {}
    user_version = 0
    if connection is not None:
        counts = {
            'conversations': _count(connection, 'SELECT COUNT(*) FROM conversations'),
            'messages': _count(connection, 'SELECT COUNT(*) FROM messages'),
            'memories': _count(connection, 'SELECT COUNT(*) FROM memories'),
            'active_memories': _count(
                connection, 'SELECT COUNT(*) FROM memories WHERE deleted_at IS NULL'),
            'memory_jobs_pending': _count(
                connection, "SELECT COUNT(*) FROM memory_jobs WHERE status = 'pending'"),
            'memory_conflicts_open': _count(
                connection, "SELECT COUNT(*) FROM memory_conflicts WHERE status = 'open'"),
            'growth_candidates_pending': _count(
                connection, "SELECT COUNT(*) FROM growth_candidates WHERE status = 'pending'"),
            'usage_rows': _count(connection, 'SELECT COUNT(*) FROM usage_ledger'),
        }
        user_version = _count(connection, 'PRAGMA user_version')
    settings = getattr(store, 'settings', None)
    warnings = getattr(store, 'warnings', None)
    log = getattr(ctx, 'log', None)
    try:
        log_entries = len(log) if log is not None else 0
    except (TypeError, RuntimeError):
        log_entries = 0
    return {
        'generated_at': _utc_stamp(),
        'service_version': SERVICE_VERSION,
        'protocol_version': PROTOCOL_VERSION,
        'settings_schema_version': settings.get('schema_version')
        if isinstance(settings, dict) else None,
        'database_user_version': user_version,
        'operating_system': platform.system(),
        'operating_system_release': platform.release(),
        'machine': platform.machine(),
        'python_version': platform.python_version(),
        'sqlite_version': sqlite3.sqlite_version,
        'settings_warnings': list(warnings) if isinstance(warnings, list) else [],
        'event_log_entries': log_entries,
        'counts': counts,
    }


def _log_text(ctx):
    log = getattr(ctx, 'log', None)
    tail = getattr(log, 'tail', None)
    if not callable(tail):
        return ''
    try:
        entries = tail(LOG_TAIL)
    except (TypeError, RuntimeError):
        return ''
    lines = []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        line = ' '.join(str(entry.get(field, ''))
                        for field in ('time', 'level', 'module', 'code', 'message'))
        lines.append(_redact_text(line.rstrip()))
    return '\n'.join(lines) + ('\n' if lines else '')


def _archive(files):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, data in files:
            archive.writestr(name, data)
    return buffer.getvalue()


def _manifest(stamp, files):
    return {
        'format_version': BUNDLE_FORMAT_VERSION,
        'generated_at': stamp,
        'service_version': SERVICE_VERSION,
        'protocol_version': PROTOCOL_VERSION,
        'files': [{'name': name, 'bytes': len(data),
                   'sha256': hashlib.sha256(data).hexdigest()}
                  for name, data in files],
    }


def _write(target, data):
    temporary = target.with_name('.' + target.name + '.tmp')
    try:
        with open(temporary, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    except OSError:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def export(request, store, ctx):
    data_dir = _data_directory(store)
    if data_dir is None:
        raise ProtocolError('storage_unavailable', 'Data directory is unavailable.',
                            request['id'])
    exports = data_dir / EXPORT_DIRNAME
    if not _inside_data_dir(data_dir, exports):
        raise ProtocolError('storage_unavailable', 'Export path leaves the data directory.',
                            request['id'])
    try:
        exports.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise ProtocolError('storage_unavailable', 'Export directory cannot be created.',
                            request['id']) from None

    settings = getattr(store, 'settings', None)
    settings_snapshot = _redact(settings if isinstance(settings, dict) else {})
    stamp = _utc_stamp()
    files = [
        ('settings.json', dumps(settings_snapshot)),
        ('diagnostics.json', dumps(_diagnostics(store, ctx))),
        ('logs.txt', _log_text(ctx).encode('utf-8')),
    ]
    included = ['manifest.json'] + [name for name, _ in files]
    bundled = [('manifest.json', dumps(_manifest(stamp, files)))] + files
    data = _archive(bundled)
    if len(data) > MAX_BUNDLE_BYTES:
        # The bounded event log dominates size; keep the bundle within the cap.
        trimmed = [item for item in files if item[0] != 'logs.txt']
        trimmed.append(('logs.txt', b''))
        bundled = [('manifest.json', dumps(_manifest(stamp, trimmed)))] + trimmed
        files = trimmed
        data = _archive(bundled)
    if len(data) > MAX_BUNDLE_BYTES:
        raise ProtocolError('export_too_large',
                            'Diagnostic bundle exceeds the 2 MB limit.',
                            request['id'])

    target = exports / ('diag-' + stamp + '.zip')
    if not _inside_data_dir(data_dir, target):
        raise ProtocolError('storage_unavailable', 'Export path leaves the data directory.',
                            request['id'])
    try:
        _write(target, data)
    except OSError:
        raise ProtocolError('save_failed', 'Diagnostic bundle was not written.',
                            request['id']) from None
    return 'result', {
        'path': str(target),
        'bytes': len(data),
        'included': included,
    }


COMMANDS = {'diag.export': export}
