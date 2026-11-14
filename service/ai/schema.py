"""Validation for the assistant's structured reply.

The model must return one JSON object with ``text``, ``speak``, ``action``,
``memory_candidates``, ``state_suggestion`` and ``confidence``. Unknown action
ids are rejected so a model cannot drive an animation the app does not have.
"""

import json
import math
import re

from service.ai.actions import allowed as _allowed_actions

ALLOWED_ACTIONS = _allowed_actions()
MAX_TEXT_CHARS = 4000
MAX_SPEAK_CHARS = 2000
MAX_CANDIDATES = 8
MAX_CANDIDATE_CHARS = 500
MAX_CANDIDATE_CATEGORY_CHARS = 64
MAX_CANDIDATE_SOURCE_CHARS = 32
MAX_STATE_FIELDS = 8
STATE_KEY = re.compile(r'^[a-z][a-z0-9_]{0,31}$')
REQUIRED_FIELDS = ('text', 'speak', 'action', 'memory_candidates',
                   'state_suggestion', 'confidence')


class SchemaError(ValueError):
    """A repairable structured-result error with a stable code."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def _fail(code, message):
    raise SchemaError(code, message)


def _strip_fence(raw):
    text = raw.strip()
    if not text.startswith('```'):
        return text
    newline = text.find('\n')
    if newline == -1:
        _fail('invalid_json', 'Reply is an incomplete code fence.')
    text = text[newline + 1:]
    if text.endswith('```'):
        text = text[:-3]
    return text.strip()


def parse_structured(raw):
    """Parse and validate a provider reply; raise SchemaError on any defect."""
    if type(raw) is not str or not raw.strip():
        _fail('invalid_json', 'Reply is empty.')
    text = _strip_fence(raw)
    try:
        value = json.loads(text)
    except (ValueError, RecursionError) as exc:
        _fail('invalid_json', 'Reply is not valid JSON: ' + str(exc))
    return validate_structured(value)


def _text(value, name, max_chars, allow_empty):
    if type(value) is not str:
        _fail('invalid_type', name + ' must be a string.')
    if not value.strip() and not allow_empty:
        _fail('missing_field', name + ' must not be empty.')
    if len(value) > max_chars:
        _fail('out_of_range', name + ' exceeds ' + str(max_chars) + ' characters.')
    return value


def _action(value):
    if type(value) is not str:
        _fail('invalid_type', 'action must be a string.')
    normalized = value.strip().lower()
    if normalized not in ALLOWED_ACTIONS:
        _fail('unknown_action', 'action is not a known animation id.')
    return normalized


def _candidate(item):
    """Validate one string or structured candidate and normalize its fields.

    A plain string keeps the old text-only shape. A structured object carries
    the category/importance/source_type the memory writer and growth evidence
    use; invalid optional values are rejected so a repair retry can fix them.
    """
    if type(item) is str:
        text = item.strip()
        if not text:
            _fail('missing_field', 'memory_candidates entries must not be empty.')
        if len(text) > MAX_CANDIDATE_CHARS:
            _fail('out_of_range', 'A memory candidate is too long.')
        return text
    if not isinstance(item, dict):
        _fail('invalid_type',
              'memory_candidates entries must be strings or objects.')
    raw = item.get('text', item.get('content'))
    if type(raw) is not str:
        _fail('invalid_type',
              'A structured memory candidate needs a text field.')
    text = raw.strip()
    if not text:
        _fail('missing_field', 'memory_candidates entries must not be empty.')
    if len(text) > MAX_CANDIDATE_CHARS:
        _fail('out_of_range', 'A memory candidate is too long.')
    candidate = {'text': text}
    for key, max_chars in (
            ('category', MAX_CANDIDATE_CATEGORY_CHARS),
            ('source_type', MAX_CANDIDATE_SOURCE_CHARS)):
        value = item.get(key)
        if value is None:
            continue
        if type(value) is not str:
            _fail('invalid_type',
                  'memory_candidates ' + key + ' must be a string.')
        value = value.strip()
        if len(value) > max_chars:
            _fail('out_of_range',
                  'memory_candidates ' + key + ' is too long.')
        candidate[key] = value
    importance = item.get('importance')
    if importance is not None:
        if type(importance) not in (int, float) or isinstance(importance, bool):
            _fail('invalid_type',
                  'memory_candidates importance must be a number.')
        importance = float(importance)
        if not math.isfinite(importance) or not 0.0 <= importance <= 1.0:
            _fail('out_of_range',
                  'memory_candidates importance must be between 0 and 1.')
        candidate['importance'] = importance
    return candidate


def _candidates(value):
    if type(value) is not list:
        _fail('invalid_type', 'memory_candidates must be a list.')
    if len(value) > MAX_CANDIDATES:
        _fail('out_of_range', 'memory_candidates has too many entries.')
    return [_candidate(item) for item in value]


def _state_suggestion(value):
    if type(value) is not dict:
        _fail('invalid_type', 'state_suggestion must be an object.')
    if len(value) > MAX_STATE_FIELDS:
        _fail('out_of_range', 'state_suggestion has too many fields.')
    result = {}
    for key, item in value.items():
        if type(key) is not str or not STATE_KEY.match(key):
            _fail('invalid_type', 'state_suggestion keys must be snake_case.')
        if type(item) not in (int, float) or isinstance(item, bool):
            _fail('invalid_type', 'state_suggestion values must be numbers.')
        number = float(item)
        if not math.isfinite(number) or not -1.0 <= number <= 1.0:
            _fail('out_of_range', 'state_suggestion values must be between -1 and 1.')
        result[key] = number
    return result


def _confidence(value):
    if type(value) not in (int, float) or isinstance(value, bool):
        _fail('invalid_type', 'confidence must be a number.')
    number = float(value)
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        _fail('out_of_range', 'confidence must be between 0 and 1.')
    return number


def validate_structured(value):
    if type(value) is not dict:
        _fail('invalid_type', 'Reply must be a JSON object.')
    for field in REQUIRED_FIELDS:
        if field not in value:
            _fail('missing_field', 'Missing field: ' + field + '.')
    return {
        'text': _text(value['text'], 'text', MAX_TEXT_CHARS, allow_empty=False),
        'speak': _text(value['speak'], 'speak', MAX_SPEAK_CHARS, allow_empty=True),
        'action': _action(value['action']),
        'memory_candidates': _candidates(value['memory_candidates']),
        'state_suggestion': _state_suggestion(value['state_suggestion']),
        'confidence': _confidence(value['confidence']),
    }


def repair_instruction(error):
    """A compact repair request appended after an invalid structured reply."""
    return ('Your previous reply was invalid (%s). Reply again with exactly one '
            'JSON object using the fields text, speak, action, memory_candidates, '
            'state_suggestion and confidence. memory_candidates is a list whose '
            'entries are either strings or objects with the fields text, '
            'category, importance (0 to 1) and source_type. No markdown and no '
            'extra text. Do not mention JSON, formatting, the retry, or this '
            'instruction in the answer text; answer the user request directly.'
            % error.message)
