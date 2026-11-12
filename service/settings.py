"""Validated settings and durable, atomic, single-writer JSON persistence."""

from copy import deepcopy
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import shutil
import tempfile
import time
import uuid

from service.jsonutil import MAX_FRAME_BYTES, dumps, loads
from service.character_catalog import ALLOWED_CHARACTER_IDS
from service.persona import ALLOWED_PERSONA_IDS, DEFAULT_PERSONA_ID

SCHEMA_VERSION = 7
DEFAULTS_V1 = {
    'schema_version': 1, 'character_id': 'godette', 'scale': 1.0,
    'screen_id': '', 'user_name': '你',
    'placement': {'x': 1.0, 'y': 1.0, 'edge_x': 1, 'edge_y': 1},
}
DEFAULTS_V2 = {
    'schema_version': 2, 'character_id': 'godette',
    'persona_id': DEFAULT_PERSONA_ID, 'scale': 1.0,
    'screen_id': '', 'user_name': '你',
    'placement': {'x': 1.0, 'y': 1.0, 'edge_x': 1, 'edge_y': 1},
}
AI_EMBEDDING_DEFAULTS = {
    'enabled': False,
    'model': '',
    'dimensions': 0,
}
AI_DEFAULTS = {
    'provider': 'deepseek',
    'base_url': 'https://api.deepseek.com',
    'chat_path': '/v1/chat/completions',
    'model': 'deepseek-chat',
    'temperature': 0.8,
    'max_tokens': 800,
    'connect_timeout_ms': 5000,
    'request_timeout_ms': 30000,
    'max_retries': 1,
    'daily_token_budget': 120000,
    'monthly_cost_budget': 15.0,
    'currency': 'USD',
    'input_price_per_million': 0.0,
    'output_price_per_million': 0.0,
    'embedding': deepcopy(AI_EMBEDDING_DEFAULTS),
}
MEMORY_COMPACTION_DEFAULTS = {
    'enabled': False,
    'model_compress': False,
    'daily_limit': 20,
    'min_age_days': 7,
    'max_per_run': 10,
    'protect_importance': 0.8,
}
MEMORY_ARCHIVE_DEFAULTS = {
    'enabled': True,
    'topic_stale_days': 14,
}
MEMORY_DEFAULTS = {
    'enabled': True,
    'auto_capture': False,
    'max_injected': 5,
    'max_entry_chars': 500,
    'compaction': deepcopy(MEMORY_COMPACTION_DEFAULTS),
    'archive': deepcopy(MEMORY_ARCHIVE_DEFAULTS),
}
STYLE_DEFAULTS = {
    'casualness': 0.5,
    'warmth': 0.5,
    'directness': 0.5,
    'playfulness': 0.5,
    'sentence_length': 0.5,
    'emoji_frequency': 0.5,
}
EXPRESSION_DEFAULTS = {
    'enabled': True,
    'phrases_enabled': True,
    'style': deepcopy(STYLE_DEFAULTS),
}
AUTONOMY_DEFAULTS = {
    'enabled': False,
    'quiet_start': '23:00',
    'quiet_end': '08:00',
    'min_seconds_between': 1800,
    'max_per_hour': 2,
    'manual_quiet_until': '',
    'cloud_enabled': True,
    'default_quiet_minutes': 30,
}
BEHAVIOR_DEFAULTS = {
    'enabled': True,
}
PERFORMANCE_DEFAULTS = {
    'idle_fps': 30,
    'drag_fps': 60,
    'hidden_fps': 5,
}
DESKTOP_DEFAULTS = {
    'enabled': True,
    'poll_seconds': 3,
    'idle_after_seconds': 300,
    'fullscreen_policy': 'silent',
    'dnd_policy': 'silent',
    'work_focus_policy': 'minimal',
    'idle_policy': 'gentle',
}
PRIVACY_DEFAULTS = {
    'log_prompts': False,
}
GROWTH_DEFAULTS = {
    'enabled': False,
    'min_sessions': 3,
    'min_memories': 2,
    'max_step': 0.05,
    'cooldown_days': 7,
    'locked_traits': [],
}
VOICE_DEFAULTS = {
    # Speaking is paused: the command works, but synthesis is synchronous on the
    # service thread, so a long line blocks every other request for up to 20 s.
    # Off by default keeps that off the normal path until speech is finished.
    'enabled': False,
    'voice': '',
    'rate': 0,
    'volume': 70,
}
BUBBLE_DEFAULTS = {
    'enabled': True,
    'duration_seconds': 4,
    'show_replies': True,
}
GREETING_DEFAULTS = {
    'enabled': True,
    'on_start': True,
    'cooldown_minutes': 60,
}
GROUP_DEFAULTS = {
    'ai': AI_DEFAULTS,
    'memory': MEMORY_DEFAULTS,
    'expression': EXPRESSION_DEFAULTS,
    'autonomy': AUTONOMY_DEFAULTS,
    'privacy': PRIVACY_DEFAULTS,
    'growth': GROWTH_DEFAULTS,
    'desktop': DESKTOP_DEFAULTS,
    'voice': VOICE_DEFAULTS,
    'behavior': BEHAVIOR_DEFAULTS,
    'performance': PERFORMANCE_DEFAULTS,
    'bubble': BUBBLE_DEFAULTS,
    'greeting': GREETING_DEFAULTS,
}
DEFAULTS = {
    'schema_version': SCHEMA_VERSION, 'character_id': 'godette',
    'persona_id': DEFAULT_PERSONA_ID, 'scale': 1.0,
    'screen_id': '', 'user_name': '你',
    'placement': {'x': 1.0, 'y': 1.0, 'edge_x': 1, 'edge_y': 1},
    **deepcopy(GROUP_DEFAULTS),
}
PATCH_KEYS = DEFAULTS.keys() - {'schema_version'}
REPLACE_ATTEMPTS = 4
LEGACY_CHARACTERS = {'swordsman', 'alicia', 'soldier', 'cartoon_skeleton', 'man'}
TIME_OF_DAY = re.compile(r'^([01][0-9]|2[0-3]):[0-5][0-9]$')


class ValidationError(ValueError):
    pass


def _number(value, low, high):
    if type(value) not in (int, float) or not low <= value <= high:
        raise ValidationError('number outside allowed range or invalid type')
    return float(value)


def _integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValidationError('integer outside allowed range or invalid type')
    return value


def _boolean(value):
    if type(value) is not bool:
        raise ValidationError('expected a boolean')
    return value


def _string(value, max_length=None, allow_empty=True):
    if type(value) is not str:
        raise ValidationError('expected a string')
    try:
        value.encode('utf-8')
    except UnicodeError as exc:
        raise ValidationError('invalid Unicode string') from exc
    if not allow_empty and not value:
        raise ValidationError('expected a non-empty string')
    if max_length is not None and len(value) > max_length:
        raise ValidationError('string exceeds allowed length')
    return value


def _url(value):
    value = _string(value, 2048, allow_empty=False)
    if not (value.startswith('https://') or value.startswith('http://')):
        raise ValidationError('base_url must be an http or https URL')
    return value


def _time_of_day(value):
    value = _string(value, 5, allow_empty=False)
    if not TIME_OF_DAY.match(value):
        raise ValidationError('time must use HH:MM in 24-hour form')
    return value


def _optional_timestamp(value):
    value = _string(value, 64)
    if not value:
        return value
    try:
        datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as exc:
        raise ValidationError('manual_quiet_until must be empty or ISO 8601') from exc
    return value


def _nested_mapping(value, defaults, label, partial):
    if type(value) is not dict or value.keys() - defaults.keys():
        raise ValidationError(f'{label} contains unsupported fields')
    if not partial and value.keys() != defaults.keys():
        raise ValidationError(f'{label} settings are incomplete')
    return value


def _style(value, partial):
    _nested_mapping(value, STYLE_DEFAULTS, 'expression.style', partial)
    return {key: _number(item, 0.0, 1.0) for key, item in value.items()}


def _compaction(value, partial):
    _nested_mapping(value, MEMORY_COMPACTION_DEFAULTS, 'memory.compaction', partial)
    result = {}
    for key, item in value.items():
        if key in ('enabled', 'model_compress'):
            result[key] = _boolean(item)
        elif key == 'daily_limit':
            result[key] = _integer(item, 0, 10000)
        elif key == 'min_age_days':
            result[key] = _integer(item, 0, 3650)
        elif key == 'max_per_run':
            result[key] = _integer(item, 1, 100)
        elif key == 'protect_importance':
            result[key] = _number(item, 0.0, 1.0)
    return result


def _archive(value, partial):
    _nested_mapping(value, MEMORY_ARCHIVE_DEFAULTS, 'memory.archive', partial)
    result = {}
    for key, item in value.items():
        if key == 'enabled':
            result[key] = _boolean(item)
        elif key == 'topic_stale_days':
            result[key] = _integer(item, 1, 3650)
    return result


def _embedding(value, partial):
    _nested_mapping(value, AI_EMBEDDING_DEFAULTS, 'ai.embedding', partial)
    result = {}
    for key, item in value.items():
        if key == 'enabled':
            result[key] = _boolean(item)
        elif key == 'model':
            result[key] = _string(item, 200)
        elif key == 'dimensions':
            result[key] = _integer(item, 0, 4096)
    return result


def _traits(value):
    if type(value) is not list or len(value) > 64:
        raise ValidationError('growth.locked_traits must be a list of at most 64 traits')
    return [_string(item, 64, allow_empty=False) for item in value]


def _group(name, value, partial):
    defaults = GROUP_DEFAULTS[name]
    if type(value) is not dict or value.keys() - defaults.keys():
        raise ValidationError(f'{name} contains unsupported fields')
    if not partial and value.keys() != defaults.keys():
        raise ValidationError(f'{name} settings are incomplete')
    result = {}
    for key, item in value.items():
        if name == 'ai':
            result[key] = _validate_ai(key, item, partial)
        elif name == 'memory':
            result[key] = _validate_memory(key, item, partial)
        elif name == 'expression':
            result[key] = _validate_expression(key, item, partial)
        elif name == 'autonomy':
            result[key] = _validate_autonomy(key, item)
        elif name == 'growth':
            result[key] = _validate_growth(key, item)
        elif name == 'desktop':
            result[key] = _validate_desktop(key, item)
        elif name == 'voice':
            result[key] = _validate_voice(key, item)
        elif name == 'behavior':
            result[key] = _validate_behavior(key, item)
        elif name == 'performance':
            result[key] = _validate_performance(key, item)
        elif name == 'bubble':
            result[key] = _validate_bubble(key, item)
        elif name == 'greeting':
            result[key] = _validate_greeting(key, item)
        else:
            result[key] = _boolean(item)
    return result


def _validate_ai(key, value, partial):
    if key == 'provider':
        return _string(value, 64, allow_empty=False)
    if key == 'base_url':
        return _url(value)
    if key == 'chat_path':
        path = _string(value, 512, allow_empty=False)
        if not path.startswith('/'):
            raise ValidationError('chat_path must start with /')
        return path
    if key == 'model':
        return _string(value, 200, allow_empty=False)
    if key == 'temperature':
        return _number(value, 0.0, 2.0)
    if key == 'max_tokens':
        return _integer(value, 1, 1000000)
    if key in ('connect_timeout_ms', 'request_timeout_ms'):
        return _integer(value, 1, 600000)
    if key == 'max_retries':
        return _integer(value, 0, 10)
    if key == 'daily_token_budget':
        return _integer(value, 0, 100000000)
    if key == 'monthly_cost_budget':
        return _number(value, 0.0, 1000000000.0)
    if key == 'currency':
        currency = _string(value, 3, allow_empty=False)
        if len(currency) != 3 or not currency.isalpha() or not currency.isupper():
            raise ValidationError('currency must be a three-letter uppercase code')
        return currency
    if key in ('input_price_per_million', 'output_price_per_million'):
        return _number(value, 0.0, 1000000.0)
    if key == 'embedding':
        return _embedding(value, partial)
    raise ValidationError('unsupported ai setting')


def _validate_memory(key, value, partial):
    if key in ('enabled', 'auto_capture'):
        return _boolean(value)
    if key == 'max_injected':
        return _integer(value, 0, 50)
    if key == 'max_entry_chars':
        return _integer(value, 1, 100000)
    if key == 'compaction':
        return _compaction(value, partial)
    if key == 'archive':
        return _archive(value, partial)
    raise ValidationError('unsupported memory setting')


def _validate_growth(key, value):
    if key == 'enabled':
        return _boolean(value)
    if key == 'min_sessions':
        return _integer(value, 1, 1000)
    if key == 'min_memories':
        return _integer(value, 1, 1000)
    if key == 'max_step':
        return _number(value, 0.001, 0.2)
    if key == 'cooldown_days':
        return _integer(value, 0, 365)
    if key == 'locked_traits':
        return _traits(value)
    raise ValidationError('unsupported growth setting')


def _validate_expression(key, value, partial):
    if key in ('enabled', 'phrases_enabled'):
        return _boolean(value)
    if key == 'style':
        return _style(value, partial)
    raise ValidationError('unsupported expression setting')


def _validate_autonomy(key, value):
    if key in ('enabled', 'cloud_enabled'):
        return _boolean(value)
    if key in ('quiet_start', 'quiet_end'):
        return _time_of_day(value)
    if key == 'min_seconds_between':
        return _integer(value, 0, 86400)
    if key == 'max_per_hour':
        return _integer(value, 0, 1000)
    if key == 'manual_quiet_until':
        return _optional_timestamp(value)
    if key == 'default_quiet_minutes':
        return _integer(value, 5, 240)
    raise ValidationError('unsupported autonomy setting')


DESKTOP_POLICY_CHOICES = {
    'fullscreen_policy': ('silent', 'minimal'),
    'dnd_policy': ('silent', 'minimal'),
    'work_focus_policy': ('silent', 'minimal', 'normal'),
    'idle_policy': ('silent', 'gentle', 'normal'),
}


def _validate_desktop(key, value):
    if key == 'enabled':
        return _boolean(value)
    if key == 'poll_seconds':
        return _integer(value, 1, 60)
    if key == 'idle_after_seconds':
        return _integer(value, 30, 86400)
    if key in DESKTOP_POLICY_CHOICES:
        return _choice(value, DESKTOP_POLICY_CHOICES[key], f'desktop.{key}')
    raise ValidationError('unsupported desktop setting')


def _validate_voice(key, value):
    if key == 'enabled':
        return _boolean(value)
    if key == 'voice':
        return _string(value, 200)
    if key == 'rate':
        return _integer(value, -10, 10)
    if key == 'volume':
        return _integer(value, 0, 100)
    raise ValidationError('unsupported voice setting')


def _validate_behavior(key, value):
    if key == 'enabled':
        return _boolean(value)
    raise ValidationError('unsupported behavior setting')


def _validate_performance(key, value):
    if key == 'idle_fps':
        return _integer(value, 10, 60)
    if key == 'drag_fps':
        return _integer(value, 30, 120)
    if key == 'hidden_fps':
        return _integer(value, 1, 15)
    raise ValidationError('unsupported performance setting')


def _validate_bubble(key, value):
    if key in ('enabled', 'show_replies'):
        return _boolean(value)
    if key == 'duration_seconds':
        return _integer(value, 2, 15)
    raise ValidationError('unsupported bubble setting')


def _validate_greeting(key, value):
    if key in ('enabled', 'on_start'):
        return _boolean(value)
    if key == 'cooldown_minutes':
        return _integer(value, 5, 1440)
    raise ValidationError('unsupported greeting setting')


def _choice(value, choices, label):
    if type(value) is not str or value not in choices:
        raise ValidationError(f'{label} must be one of ' + ', '.join(choices))
    return value


def _scalar(key, value):
    if key == 'character_id':
        if type(value) is not str or value not in ALLOWED_CHARACTER_IDS:
            raise ValidationError('unknown character_id')
        return value
    if key == 'persona_id':
        if type(value) is not str or value not in ALLOWED_PERSONA_IDS:
            raise ValidationError('unknown persona_id')
        return value
    if key == 'scale':
        return _number(value, 0.5, 2.0)
    if key in ('screen_id', 'user_name'):
        _string(value, 80 if key == 'user_name' else None)
        return value
    if key == 'placement':
        if type(value) is not dict or value.keys() != {'x', 'y', 'edge_x', 'edge_y'}:
            raise ValidationError('placement requires x,y,edge_x,edge_y')
        result = {}
        for axis in ('x', 'y'):
            result[axis] = _number(value[axis], 0.0, 1.0)
        for edge in ('edge_x', 'edge_y'):
            if type(value[edge]) is not int or not -1 <= value[edge] <= 1:
                raise ValidationError('placement edges must be integers from -1 to 1')
            result[edge] = value[edge]
        return result
    raise ValidationError('unsupported setting')


def validate_patch(patch):
    if type(patch) is not dict or patch.keys() - PATCH_KEYS:
        raise ValidationError('settings patch contains unsupported fields')
    result = {}
    for key, value in patch.items():
        if key in GROUP_DEFAULTS:
            result[key] = _group(key, value, partial=True)
        else:
            result[key] = _scalar(key, value)
    return result


def validate_document(document):
    if (type(document) is not dict or document.keys() != DEFAULTS.keys()
            or type(document.get('schema_version')) is not int
            or document['schema_version'] != SCHEMA_VERSION):
        raise ValidationError('unsupported or incomplete settings schema')
    normalized = {'schema_version': SCHEMA_VERSION}
    for key, value in document.items():
        if key == 'schema_version':
            continue
        if key in GROUP_DEFAULTS:
            normalized[key] = _group(key, value, partial=False)
        else:
            normalized[key] = _scalar(key, value)
    # Leave room for the ready/result envelope, request ID and recovery warnings.
    if len(dumps(normalized)) > MAX_FRAME_BYTES - 8192:
        raise ValidationError('settings exceed the protocol response budget')
    return normalized


def _add_default_groups(document):
    """Fill every settings group from defaults; used by the v1/v2 paths."""
    document['schema_version'] = SCHEMA_VERSION
    for key, value in GROUP_DEFAULTS.items():
        document[key] = deepcopy(value)
    return document


# v3 had every group except growth, desktop, voice, behavior, performance,
# bubble and greeting.
V3_TOP_LEVEL_KEYS = DEFAULTS.keys() - {
    'growth', 'desktop', 'voice', 'behavior', 'performance',
    'bubble', 'greeting',
}
# v4 added growth but neither the desktop/behavior/performance groups nor
# autonomy.cloud_enabled. The voice group was added to v5 later, so a v4
# document may or may not carry it.
V4_TOP_LEVEL_KEYS = DEFAULTS.keys() - {
    'desktop', 'behavior', 'performance', 'bubble', 'greeting',
}
V4_TOP_LEVEL_KEYS_WITHOUT_VOICE = V4_TOP_LEVEL_KEYS - {'voice'}
V4_AUTONOMY_KEYS = frozenset({
    'enabled', 'quiet_start', 'quiet_end', 'min_seconds_between',
    'max_per_hour', 'manual_quiet_until',
})
# v5 added the desktop group and autonomy.cloud_enabled. The voice group was
# added to v5 after the fact, so a v5 document may or may not carry it.
V5_TOP_LEVEL_KEYS = DEFAULTS.keys() - {'behavior', 'performance', 'bubble', 'greeting'}
V5_TOP_LEVEL_KEYS_WITHOUT_VOICE = V5_TOP_LEVEL_KEYS - {'voice'}
# v6 added the behavior and performance groups; v7 adds bubble and greeting.
V6_TOP_LEVEL_KEYS = DEFAULTS.keys() - {'bubble', 'greeting'}


def _add_missing_groups(document):
    """Add the v4..v7 groups while preserving every value already stored."""
    migrated = deepcopy(document)
    migrated['schema_version'] = SCHEMA_VERSION
    for name in ('voice', 'behavior', 'performance', 'bubble', 'greeting'):
        migrated.setdefault(name, deepcopy(GROUP_DEFAULTS[name]))
    return migrated


def _upgrade_v3(document):
    """Add v4/v5/v6 fields while preserving every value already stored in v3."""
    migrated = _add_missing_groups(document)
    migrated.setdefault('growth', deepcopy(GROWTH_DEFAULTS))
    migrated.setdefault('desktop', deepcopy(DESKTOP_DEFAULTS))
    memory = migrated.get('memory')
    if type(memory) is dict:
        memory.setdefault('compaction', deepcopy(MEMORY_COMPACTION_DEFAULTS))
        memory.setdefault('archive', deepcopy(MEMORY_ARCHIVE_DEFAULTS))
    ai = migrated.get('ai')
    if type(ai) is dict:
        ai.setdefault('embedding', deepcopy(AI_EMBEDDING_DEFAULTS))
    autonomy = migrated.get('autonomy')
    if type(autonomy) is dict:
        autonomy.setdefault('cloud_enabled', AUTONOMY_DEFAULTS['cloud_enabled'])
        autonomy.setdefault('default_quiet_minutes',
                            AUTONOMY_DEFAULTS['default_quiet_minutes'])
    return migrated


def _upgrade_v4(document):
    """Add the desktop group and the new groups/keys to a v4 document."""
    migrated = _add_missing_groups(document)
    migrated.setdefault('desktop', deepcopy(DESKTOP_DEFAULTS))
    autonomy = migrated.get('autonomy')
    if type(autonomy) is dict:
        autonomy.setdefault('cloud_enabled', AUTONOMY_DEFAULTS['cloud_enabled'])
        autonomy.setdefault('default_quiet_minutes',
                            AUTONOMY_DEFAULTS['default_quiet_minutes'])
    return migrated


def _upgrade_v5(document):
    """Add the v6 groups and autonomy.default_quiet_minutes to a v5 document.

    The voice group was added to schema v5 after the fact, so a v5 file may
    already carry it; it must survive untouched.
    """
    migrated = _add_missing_groups(document)
    autonomy = migrated.get('autonomy')
    if type(autonomy) is dict:
        autonomy.setdefault('default_quiet_minutes',
                            AUTONOMY_DEFAULTS['default_quiet_minutes'])
    return migrated


def _upgrade_v6(document):
    """Add the v7 bubble and greeting groups to a v6 document.

    Every value already stored stays as it is; only the two missing groups are
    filled from their defaults.
    """
    return _add_missing_groups(document)


def migrate_document(document):
    """Upgrade a recognized older schema; return (document, migrated)."""
    if type(document) is not dict:
        return document, False
    if (document.get('schema_version') == 1
            and document.keys() == DEFAULTS_V1.keys()):
        migrated = deepcopy(document)
        migrated['persona_id'] = DEFAULT_PERSONA_ID
        return _add_default_groups(migrated), True
    if (document.get('schema_version') == 2
            and document.keys() == DEFAULTS_V2.keys()):
        return _add_default_groups(deepcopy(document)), True
    if (document.get('schema_version') == 3
            and document.keys() == V3_TOP_LEVEL_KEYS):
        return _upgrade_v3(document), True
    if (document.get('schema_version') == 4
            and document.keys() in (V4_TOP_LEVEL_KEYS, V4_TOP_LEVEL_KEYS_WITHOUT_VOICE)
            and type(document.get('autonomy')) is dict
            and document['autonomy'].keys() == V4_AUTONOMY_KEYS):
        return _upgrade_v4(document), True
    if (document.get('schema_version') == 5
            and document.keys() in (V5_TOP_LEVEL_KEYS,
                                    V5_TOP_LEVEL_KEYS_WITHOUT_VOICE)):
        return _upgrade_v5(document), True
    if (document.get('schema_version') == 6
            and document.keys() == V6_TOP_LEVEL_KEYS):
        return _upgrade_v6(document), True
    return document, False


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


def _deep_merge(base, patch):
    """Recursively merge partial nested groups without dropping sibling keys."""
    result = deepcopy(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


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
        migrated_character = False
        migrated_schema = False
        try:
            if len(raw) > MAX_FRAME_BYTES:
                raise ValidationError('config too large')
            document = loads(raw)
            if (type(document) is dict and type(document.get('character_id')) is str
                    and document['character_id'] in LEGACY_CHARACTERS):
                document = {**document, 'character_id': 'godette'}
                migrated_character = True
            document, migrated_schema = migrate_document(document)
            self._settings = validate_document(document)
        except (ValueError, UnicodeError, RecursionError):
            backup = self._backup()
            defaults = deepcopy(DEFAULTS)
            atomic_write(self.path, dumps(defaults) + b'\n')
            self._settings = defaults
            self.warnings.append('Invalid settings backed up to ' + backup.name + '; defaults restored.')
            return
        if migrated_character or migrated_schema:
            atomic_write(self.path, dumps(self._settings) + b'\n')
        if migrated_character:
            self.warnings.append('Character changed to Godette; all other settings preserved.')
        if migrated_schema:
            self.warnings.append(f'Settings migrated to schema version {SCHEMA_VERSION}; '
                                 'missing defaults added.')

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
        validated = validate_patch(patch)
        merged = deepcopy(self._settings)
        for key, value in validated.items():
            if key in GROUP_DEFAULTS:
                merged[key] = _deep_merge(merged[key], value)
            else:
                merged[key] = value
        candidate = validate_document(merged)
        atomic_write(self.path, dumps(candidate) + b'\n')
        self._settings = candidate
        return self.settings
