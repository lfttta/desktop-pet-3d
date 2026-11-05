"""Local expression phrase library with cooldowns and daily limits.

The default library lives in ``data/expression/phrases.json``. The user
override lives under the data directory at ``expression/phrases.local.json``;
it may update fields of an existing phrase, add new phrases, or disable phrases
by id. Selection never picks a disabled phrase, a phrase still in cooldown, or
one that reached its daily limit.

``phrase_usage.uses`` is the number of uses on the day recorded in
``last_used_at``; it resets when the day changes. This keeps the existing
schema unchanged while still enforcing a per-day limit.
"""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import random
import tempfile

DEFAULT_LIBRARY = Path(__file__).resolve().parents[2] / 'data' / 'expression' / 'phrases.json'
USER_LIBRARY_NAME = 'phrases.local.json'


def user_library_path(data_dir):
    """Return the user override path inside one data directory."""
    return Path(data_dir) / 'expression' / USER_LIBRARY_NAME


def _read_json(path):
    with Path(path).open('rb') as stream:
        return json.loads(stream.read().decode('utf-8'))


def _write_json(path, document):
    content = json.dumps(document, ensure_ascii=False, indent=2,
                         allow_nan=False).encode('utf-8') + b'\n'
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix='.phrases-', suffix='.tmp',
                                        dir=str(path.parent))
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _positive_int(value, default=0):
    if type(value) is int and value >= 0:
        return value
    return default


def _normalize(entry):
    if type(entry) is not dict:
        return None
    phrase_id = entry.get('id')
    text = entry.get('text')
    if type(phrase_id) is not str or not phrase_id.strip():
        return None
    if type(text) is not str or not text.strip():
        return None
    category = entry.get('category')
    if type(category) is not str or not category:
        category = 'general'
    return {
        'id': phrase_id.strip(),
        'text': text,
        'category': category,
        'cooldown_seconds': _positive_int(entry.get('cooldown_seconds')),
        'daily_limit': _positive_int(entry.get('daily_limit')),
        'enabled': entry.get('enabled', True) is not False,
    }


def _merge(base, override):
    merged = dict(base)
    for key in ('text', 'category'):
        value = override.get(key)
        if type(value) is str and value.strip():
            merged[key] = value
    for key in ('cooldown_seconds', 'daily_limit'):
        if type(override.get(key)) is int and override[key] >= 0:
            merged[key] = override[key]
    if type(override.get('enabled')) is bool:
        merged['enabled'] = override['enabled']
    return merged


def _user_path(user_path, data_dir):
    if user_path is not None:
        return Path(user_path)
    if data_dir is not None:
        return user_library_path(data_dir)
    return None


def _data_dir_from_connection(target):
    """Derive the data directory from the database file when possible."""
    path = getattr(target, 'path', None)
    if path is not None:
        return Path(path).parent
    try:
        rows = _connection(target).execute('PRAGMA database_list').fetchall()
    except Exception:
        return None
    for row in rows:
        if len(row) >= 3 and row[1] == 'main' and row[2]:
            return Path(row[2]).parent
    return None


def load_phrases(default_path=None, user_path=None, data_dir=None):
    """Load the default library and apply the optional user override."""
    default_path = Path(default_path) if default_path else DEFAULT_LIBRARY
    user_path = _user_path(user_path, data_dir)
    document = _read_json(default_path)
    entries = document.get('phrases') if isinstance(document, dict) else None
    if type(entries) is not list:
        raise ValueError('phrase library must contain a phrases list')

    library = {}
    for entry in entries:
        normalized = _normalize(entry)
        if normalized is not None:
            library[normalized['id']] = normalized

    local_ids = set()
    disabled = set()
    if user_path is not None and user_path.is_file():
        try:
            user_document = _read_json(user_path)
        except (OSError, ValueError, UnicodeError):
            user_document = {}
        if isinstance(user_document, dict):
            user_entries = user_document.get('phrases')
            if type(user_entries) is list:
                for entry in user_entries:
                    if type(entry) is not dict:
                        continue
                    phrase_id = entry.get('id')
                    if type(phrase_id) is not str or not phrase_id.strip():
                        continue
                    phrase_id = phrase_id.strip()
                    local_ids.add(phrase_id)
                    if phrase_id in library:
                        library[phrase_id] = _merge(library[phrase_id], entry)
                    else:
                        normalized = _normalize(entry)
                        if normalized is not None:
                            library[phrase_id] = normalized
            user_disabled = user_document.get('disabled')
            if type(user_disabled) is list:
                disabled = {item for item in user_disabled if type(item) is str}
                local_ids |= disabled

    for phrase_id in disabled:
        if phrase_id in library:
            library[phrase_id] = dict(library[phrase_id], enabled=False)
    result = []
    for key in sorted(library):
        item = dict(library[key])
        item['source'] = 'local' if key in local_ids else 'default'
        result.append(item)
    return result


def list_phrases(data_dir=None, default_path=None):
    """Return every phrase with its effective state and source."""
    return load_phrases(default_path=default_path, data_dir=data_dir)


def _read_user_document(path):
    if not path.is_file():
        return {}
    try:
        document = _read_json(path)
    except (OSError, ValueError, UnicodeError):
        return {}
    return document if isinstance(document, dict) else {}


def set_enabled(phrase_id, enabled, data_dir=None, user_path=None):
    """Persist one phrase's enabled flag to the user override file."""
    if type(phrase_id) is not str or not phrase_id.strip():
        raise ValueError('phrase_id must be a non-empty string')
    if type(enabled) is not bool:
        raise ValueError('enabled must be a boolean')
    phrase_id = phrase_id.strip()
    current = load_phrases(user_path=user_path, data_dir=data_dir)
    if not any(item['id'] == phrase_id for item in current):
        raise KeyError(phrase_id)

    path = _user_path(user_path, data_dir)
    if path is None:
        raise ValueError('data_dir is required to save phrase preferences')
    document = _read_user_document(path)
    version = document.get('version')
    if type(version) is not int or version < 1:
        document['version'] = 1
    entries = document.get('phrases')
    if type(entries) is not list:
        entries = []
    updated = False
    for index, entry in enumerate(entries):
        if type(entry) is dict and entry.get('id') == phrase_id:
            replacement = dict(entry)
            replacement['enabled'] = enabled
            entries[index] = replacement
            updated = True
            break
    if not updated:
        entries.append({'id': phrase_id, 'enabled': enabled})
    document['phrases'] = entries

    disabled = {item for item in document.get('disabled', []) if type(item) is str}
    if enabled:
        disabled.discard(phrase_id)
    else:
        disabled.add(phrase_id)
    if disabled:
        document['disabled'] = sorted(disabled)
    else:
        document.pop('disabled', None)

    _write_json(path, document)
    for item in load_phrases(user_path=path):
        if item['id'] == phrase_id:
            return item
    return {'id': phrase_id, 'enabled': enabled, 'source': 'local'}


def normalize_now(value=None):
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_timestamp(value):
    if type(value) is not str or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _connection(connection):
    return getattr(connection, 'connection', connection)


def _usage(connection, phrase_id):
    connection = _connection(connection)
    if connection is None:
        return None
    return connection.execute(
        'SELECT uses, last_used_at FROM phrase_usage WHERE phrase_id=?',
        (phrase_id,)).fetchone()


def usable_now(connection, phrase, now=None):
    """Return True when cooldown and daily limit allow this phrase right now."""
    now = normalize_now(now)
    row = _usage(connection, phrase['id'])
    if row is None:
        return True
    uses, last_used = row
    last_at = _parse_timestamp(last_used)
    if last_at is not None and last_at.date() == now.date():
        if phrase['daily_limit'] and uses >= phrase['daily_limit']:
            return False
    else:
        uses = 0
    cooldown = phrase['cooldown_seconds']
    if cooldown and last_at is not None:
        if (now - last_at).total_seconds() < cooldown:
            return False
    return True


def _record_usage(connection, phrase, now):
    connection = _connection(connection)
    if connection is None:
        return
    row = _usage(connection, phrase['id'])
    uses = 0
    if row is not None:
        prior_uses, last_used = row
        last_at = _parse_timestamp(last_used)
        if last_at is not None and last_at.date() == now.date():
            uses = prior_uses
    connection.execute(
        'INSERT INTO phrase_usage(phrase_id, uses, last_used_at) VALUES (?, ?, ?) '
        'ON CONFLICT(phrase_id) DO UPDATE SET '
        'uses=excluded.uses, last_used_at=excluded.last_used_at',
        (phrase['id'], uses + 1, now.isoformat()))


def select_phrase(connection, category=None, now=None, rng=None,
                  default_path=None, user_path=None, data_dir=None):
    """Pick one usable phrase, record the use and return it, or return None."""
    now = normalize_now(now)
    if user_path is None and data_dir is None:
        data_dir = _data_dir_from_connection(connection)
    candidates = []
    for phrase in load_phrases(default_path=default_path, user_path=user_path,
                               data_dir=data_dir):
        if not phrase['enabled']:
            continue
        if category is not None and phrase['category'] != category:
            continue
        if not usable_now(connection, phrase, now=now):
            continue
        candidates.append(phrase)
    if not candidates:
        return None
    picker = rng.choice if rng is not None and hasattr(rng, 'choice') else random.choice
    chosen = picker(candidates)
    _record_usage(connection, chosen, now)
    return dict(chosen)
