"""流式回复中 JSON ``text`` 字段的增量提取。

旧实现每个分片都重新解析整段累计文本（O(n²)）；这里只解析新到达的后缀，
维护标记搜索与字符串解码两个状态。跨尝试的语义保持不变：``emitted`` 在
``start_attempt`` 之后保留，新尝试只有在其解码前缀严格延续 ``emitted`` 时
才继续产出增量，否则本次尝试不再产出。
"""

import re

_TEXT_MARKER = re.compile(r'(?:^|[,{])\s*"text"\s*:\s*"')
_SIMPLE_ESCAPES = {'"': '"', '\\': '\\', '/': '/', 'b': '\b',
                   'f': '\f', 'n': '\n', 'r': '\r', 't': '\t'}
_HEX4 = re.compile(r'[0-9a-fA-F]{4}')


class TextGate:
    """Best-effort incremental extraction of the JSON ``text`` value."""

    def __init__(self):
        self.emitted = ''
        self._buffer = ''
        self._in_string = False
        self._finished = False
        self._diverged = False
        self._matched = 0

    def start_attempt(self):
        self._buffer = ''
        self._in_string = False
        self._finished = False
        self._diverged = False
        self._matched = 0

    def feed(self, chunk):
        if self._diverged or self._finished or not chunk:
            return ''
        self._buffer += chunk
        if not self._in_string:
            marker = _TEXT_MARKER.search(self._buffer)
            if marker is None:
                self._trim_marker_buffer()
                return ''
            self._buffer = self._buffer[marker.end():]
            self._in_string = True
        decoded, consumed, closed = _decode_incremental(self._buffer)
        self._buffer = self._buffer[consumed:]
        if closed:
            self._finished = True
        out = []
        for char in decoded:
            if self._matched < len(self.emitted):
                if char != self.emitted[self._matched]:
                    self._diverged = True
                    return ''
                self._matched += 1
            else:
                out.append(char)
        if not out:
            return ''
        delta = ''.join(out)
        self.emitted += delta
        return delta

    def _trim_marker_buffer(self):
        """只保留可能构成标记前缀的尾部，避免累计文本无限增长。"""
        cut = max(self._buffer.rfind('{'), self._buffer.rfind(','))
        if cut > 0:
            self._buffer = self._buffer[cut:]


def _decode_incremental(buffer):
    """解码缓冲区开头的 JSON 字符串内容。

    返回 (已解码文本, 消耗的字符数, 是否遇到闭引号)。结尾的不完整转义
    留在缓冲区里等待下一个分片。
    """
    result = []
    i = 0
    length = len(buffer)
    while i < length:
        char = buffer[i]
        if char == '"':
            return ''.join(result), i + 1, True
        if char != '\\':
            result.append(char)
            i += 1
            continue
        if i + 1 >= length:
            break
        escape = buffer[i + 1]
        if escape in _SIMPLE_ESCAPES:
            result.append(_SIMPLE_ESCAPES[escape])
            i += 2
            continue
        if escape != 'u':
            break
        if i + 6 > length:
            break
        code = buffer[i + 2:i + 6]
        if not _HEX4.fullmatch(code):
            break
        escape_start = i
        number = int(code, 16)
        i += 6
        if 0xD800 <= number <= 0xDBFF:
            combined = False
            if i + 6 <= length and buffer[i:i + 2] == '\\u':
                low_code = buffer[i + 2:i + 6]
                if _HEX4.fullmatch(low_code):
                    low = int(low_code, 16)
                    if 0xDC00 <= low <= 0xDFFF:
                        number = 0x10000 + ((number - 0xD800) << 10) + (low - 0xDC00)
                        i += 6
                        combined = True
            if not combined:
                if length - i < 6:
                    # 低代理可能还在下一个分片里：把高代理转义留在缓冲区。
                    i = escape_start
                    break
                continue
        elif 0xDC00 <= number <= 0xDFFF:
            continue
        result.append(chr(number))
    return ''.join(result), i, False