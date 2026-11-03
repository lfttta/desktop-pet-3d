"""Validated settings and durable, atomic, single-writer JSON persistence."""

from copy import deepcopy
from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
import tempfile
import time
import uuid

from service.jsonutil import MAX_FRAME_BYTES, dumps, loads
from service.character_catalog import ALLOWED_CHARACTER_IDS

DEFAULTS = {
    'schema_version': 1, 'character_id': 'godette', 'scale': 1.0,
    'screen_id': '', 'user_name': '你',
    'placement': {'x': 1.0, 'y': 1.0, 'edge_x': 1, 'edge_y': 1},
}
PATCH_KEYS = DEFAULTS.keys() - {'schema_version'}
REPLACE_ATTEMPTS = 4
LEGACY_CHARACTERS = {'swordsman', 'alicia', 'soldier', 'cartoon_skeleton', 'man'}


class ValidationError(ValueError):
    pass


def _number(value, low, high):
    if type(value) not in (int, float) or not low <= value <= high:
        raise ValidationError('number outside allowed range or invalid type')
    return float(value)


def _string(value):
    if type(value) is not str:
        raise ValidationError('expected a string')
    try:
        value.encode('utf-8')
    except UnicodeError as exc:
        raise ValidationError('invalid Unicode string') from exc
    return value


def validate_patch(patch):
    if type(patch) is not dict or patch.keys() - PATCH_KEYS:
        raise ValidationError('settings patch contains unsupported fields')
    result = deepcopy(patch)
    for key, value in patch.items():
        if key == 'character_id':
            if type(value) is not str or value not in ALLOWED_CHARACTER_IDS:
                raise ValidationError('unknown character_id')
        elif key == 'scale':
            result[key] = _number(value, 0.5, 2.0)
        elif key in ('screen_id', 'user_name'):
            _string(value)
            if key == 'user_name' and len(value) > 80:
                raise ValidationError('user_name exceeds 80 characters')
        elif key == 'placement':
            if type(value) is not dict or value.keys() != {'x', 'y', 'edge_x', 'edge_y'}:
                raise ValidationError('placement requires x,y,edge_x,edge_y')
            for axis in ('x', 'y'):
                result[key][axis] = _number(value[axis], 0.0, 1.0)
            for edge in ('edge_x', 'edge_y'):
                if type(value[edge]) is not int or not -1 <= value[edge] <= 1:
                    raise ValidationError('placement edges must be integers from -1 to 1')
    return result


def validate_document(document):
    if (type(document) is not dict or document.keys() != DEFAULTS.keys()
            or type(document.get('schema_version')) is not int
            or document['schema_version'] != 1):
        raise ValidationError('unsupported or incomplete settings schema')
    patch = {key: value for key, value in document.items() if key != 'schema_version'}
    normalized = {'schema_version': 1, **validate_patch(patch)}
    # Leave room for the ready/result envelope, request ID and recovery warnings.
    if len(dumps(normalized)) > MAX_FRAME_BYTES - 8192:
        raise ValidationError('settings exceed the protocol response budget')
    return normalized


def atomic_write(path, content):
    """Only return after fsync + replacement; never replace the old file on failure."""
    descriptor, name = tempfile.mkstemp(prefix='.settings-', suffix='.tmp', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        for attempt in range(REPLACE_ATTEMPTS):
            try:
                os.replace(temporary, path)
                return
            except OSError:
                if attempt == REPLACE_ATTEMPTS - 1:
                    raise
                time.sleep(0.025 * (attempt + 1))
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            # A failed cleanup must not mask the original failed save.
            pass


class SettingsStore:
    def __init__(self, directory):
        self.path = Path(directory) / 'settings.json'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.warnings = []
        try:
            with self.path.open('rb') as stream:
                raw = stream.read(MAX_FRAME_BYTES + 1)
        except FileNotFoundError:
            self._settings = deepcopy(DEFAULTS)
            atomic_write(self.path, dumps(self._settings) + b'\n')
            return
        migrated = False
        try:
            if len(raw) > MAX_FRAME_BYTES:
                raise ValidationError('config too large')
            document = loads(raw)
            if (type(document) is dict and type(document.get('character_id')) is str
                    and document['character_id'] in LEGACY_CHARACTERS):
                document = {**document, 'character_id': 'godette'}
                migrated = True
            self._settings = validate_document(document)
        except (ValueError, UnicodeError, RecursionError):
            backup = self._backup()
            defaults = deepcopy(DEFAULTS)
            atomic_write(self.path, dumps(defaults) + b'\n')
            self._settings = defaults
            self.warnings.append('Invalid settings backed up to ' + backup.name + '; defaults restored.')
            return
        if migrated:
            atomic_write(self.path, dumps(self._settings) + b'\n')
            self.warnings.append('Character changed to Godette; all other settings preserved.')

    def _backup(self):
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
        backup = self.path.with_name(self.path.name + '.broken-' + stamp + '-' + uuid.uuid4().hex)
        # Copy bytes unchanged; exclusive creation prevents overwriting older backups.
        with self.path.open('rb') as source, backup.open('xb') as target:
            shutil.copyfileobj(source, target, length=64 * 1024)
            target.flush()
            os.fsync(target.fileno())
        return backup

    @property
    def settings(self):
        return deepcopy(self._settings)

    def update(self, patch):
        candidate = validate_document({**self._settings, **validate_patch(patch)})
        atomic_write(self.path, dumps(candidate) + b'\n')
        self._settings = candidate
        return self.settings
