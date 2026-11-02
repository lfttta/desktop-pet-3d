"""Strict JSON shared by config and protocol readers."""

import json
import math

MAX_FRAME_BYTES = 1024 * 1024


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate object key')
        result[key] = value
    return result


def _constant(value):
    raise ValueError('non-finite JSON number')


def _float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError('non-finite JSON number')
    return number


def _check_depth(raw):
    # Bound parser stack use before json.loads; brackets inside strings do not count.
    depth = 0
    quoted = escaped = False
    for byte in raw:
        if quoted:
            if escaped:
                escaped = False
            elif byte == 92:
                escaped = True
            elif byte == 34:
                quoted = False
        elif byte == 34:
            quoted = True
        elif byte in (91, 123):
            depth += 1
            if depth > 64:
                raise ValueError('JSON nesting exceeds 64 levels')
        elif byte in (93, 125):
            depth -= 1


def loads(raw):
    _check_depth(raw)
    return json.loads(raw.decode('utf-8'), object_pairs_hook=_object,
                      parse_constant=_constant, parse_float=_float)


def dumps(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False,
                      separators=(',', ':')).encode('utf-8')
