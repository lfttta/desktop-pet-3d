"""OpenAI-compatible chat completions over urllib with SSE streaming.

A transport only opens the connection and exposes ``readline``/``read``/``close``;
tests inject a fake transport so no real network call is ever required. Errors
are normalized to ``ProviderError(code, message, retryable, status)`` with
timeouts separate from connection failures.
"""

from dataclasses import dataclass
import errno
import json
import socket
import urllib.error
import urllib.request

from service.ai.budget import parse_usage
from service.jsonutil import dumps

MAX_LINE_BYTES = 256 * 1024
MAX_BODY_BYTES = 4 * 1024 * 1024
MAX_ERROR_MESSAGE_CHARS = 200


class ProviderError(Exception):
    def __init__(self, code, message, retryable=False, status=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = bool(retryable)
        self.status = status


class TransportError(Exception):
    """Raised by a transport; ``kind`` is timeout, connection or http."""

    def __init__(self, kind, message, status=None, body=b''):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.status = status
        self.body = body


@dataclass
class Completion:
    text: str
    usage: dict
    model: str = ''
    finish_reason: str = ''


def _status_error(status, body=b''):
    message = _error_message(body)
    if status == 402:
        return ProviderError('payment_required',
                             message or 'Payment is required.', False, status)
    if status in (401, 403):
        return ProviderError('auth_error', message or 'Authentication failed.',
                             False, status)
    if status == 404:
        return ProviderError('model_not_found', message or 'Model not found.',
                             False, status)
    if status == 408:
        return ProviderError('timeout', message or 'The provider timed out.',
                             True, status)
    if status == 429:
        return ProviderError('rate_limited', message or 'Rate limited.', True, status)
    if 500 <= status <= 599:
        return ProviderError('server_error',
                             message or 'The provider failed.', True, status)
    if 400 <= status <= 499:
        return ProviderError('provider_rejected',
                             message or 'The provider rejected the request.',
                             False, status)
    return ProviderError('provider_error', message or 'Provider error.', False, status)


def _error_message(body):
    if not body:
        return ''
    try:
        value = json.loads(body.decode('utf-8', errors='replace'))
    except (ValueError, UnicodeError):
        return ''
    if not isinstance(value, dict):
        return ''
    error = value.get('error')
    if isinstance(error, dict):
        text = error.get('message') or error.get('type') or ''
    else:
        text = error or value.get('message') or ''
    if not isinstance(text, str):
        return ''
    return text.strip()[:MAX_ERROR_MESSAGE_CHARS]


def _header(response, name):
    headers = getattr(response, 'headers', None)
    if headers is None:
        return ''
    try:
        value = headers.get(name)
        if value is None:
            value = headers.get(name.lower())
    except AttributeError:
        return ''
    return value if isinstance(value, str) else ''


class UrllibTransport:
    """Default transport; separates the connect deadline from the read one."""

    def send(self, url, headers, body, connect_timeout, read_timeout):
        request = urllib.request.Request(url, data=body, headers=headers,
                                         method='POST')
        try:
            response = urllib.request.urlopen(
                request, timeout=max(0.001, float(connect_timeout)))
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read(8192)
            except Exception:
                detail = b''
            raise TransportError('http', 'HTTP ' + str(exc.code), exc.code,
                                 detail) from None
        except urllib.error.URLError as exc:
            reason = exc.reason
            if isinstance(reason, (socket.timeout, TimeoutError)):
                raise TransportError('timeout', 'Connection timed out.') from None
            raise TransportError('connection', 'Connection failed.') from None
        except (socket.timeout, TimeoutError):
            raise TransportError('timeout', 'Connection timed out.') from None
        except OSError:
            raise TransportError('connection', 'Connection failed.') from None
        self._set_read_timeout(response, read_timeout)
        return response

    @staticmethod
    def _set_read_timeout(response, seconds):
        seconds = max(0.001, float(seconds))
        target = getattr(response, 'fp', None)
        raw = getattr(target, 'raw', None)
        sock = getattr(raw, '_sock', None)
        if sock is not None:
            try:
                sock.settimeout(seconds)
                return
            except OSError:
                pass
        sock = getattr(target, '_sock', None)
        if sock is not None:
            try:
                sock.settimeout(seconds)
            except OSError:
                pass


class OpenAICompatibleProvider:
    def __init__(self, ai_settings, transport=None):
        settings = ai_settings if isinstance(ai_settings, dict) else {}
        self.base_url = str(settings.get('base_url') or 'https://api.deepseek.com').rstrip('/')
        path = str(settings.get('chat_path') or '/v1/chat/completions')
        self.chat_path = path if path.startswith('/') else '/' + path
        self.model = str(settings.get('model') or 'deepseek-chat')
        self.temperature = float(settings.get('temperature', 0.8))
        self.max_tokens = int(settings.get('max_tokens', 800))
        self.connect_timeout = max(0.001, int(settings.get('connect_timeout_ms', 5000)) / 1000.0)
        self.request_timeout = max(0.001, int(settings.get('request_timeout_ms', 30000)) / 1000.0)
        self.transport = transport or UrllibTransport()

    @property
    def endpoint(self):
        return self.base_url + self.chat_path

    def _headers(self, key):
        return {
            'Authorization': 'Bearer ' + key,
            'Content-Type': 'application/json',
            'Accept': 'text/event-stream, application/json',
        }

    def _open(self, payload, key, cancel_event):
        if cancel_event is not None and cancel_event.is_set():
            raise ProviderError('cancelled', 'The request was cancelled.', False)
        if type(key) is not str or not key:
            raise ProviderError('missing_key', 'No API key is configured.', False)
        try:
            return self.transport.send(
                self.endpoint, self._headers(key), dumps(payload),
                connect_timeout=self.connect_timeout,
                read_timeout=self.request_timeout)
        except TransportError as exc:
            raise self._translate(exc) from None
        except (socket.timeout, TimeoutError):
            raise ProviderError('timeout', 'The provider timed out.', True) from None
        except OSError:
            raise ProviderError('connection_error',
                                'The provider could not be reached.', True) from None

    @staticmethod
    def _translate(exc):
        if exc.kind == 'timeout':
            return ProviderError('timeout', 'The provider timed out.', True)
        if exc.kind == 'connection':
            return ProviderError('connection_error',
                                 'The provider could not be reached.', True)
        if exc.kind == 'http':
            return _status_error(exc.status, exc.body)
        return ProviderError('provider_error', 'Provider error.', False)

    def _payload(self, messages, stream, max_tokens=None):
        return {
            'model': self.model,
            'messages': messages,
            'stream': stream,
            'temperature': self.temperature,
            'max_tokens': self.max_tokens if max_tokens is None else max_tokens,
        }

    def stream_chat(self, messages, key, on_delta=None, cancel_event=None):
        response = self._open(self._payload(messages, True), key, cancel_event)
        try:
            content_type = _header(response, 'Content-Type').lower()
            if 'application/json' in content_type and 'event-stream' not in content_type:
                completion = self._read_json(response)
                if completion.text and on_delta is not None:
                    on_delta(completion.text)
                return completion
            return self._read_stream(response, on_delta, cancel_event)
        finally:
            close = getattr(response, 'close', None)
            if close is not None:
                try:
                    close()
                except Exception:
                    pass

    def probe(self, key):
        response = self._open(self._payload(
            [{'role': 'user', 'content': 'ping'}], False, max_tokens=1), key, None)
        try:
            content_type = _header(response, 'Content-Type').lower()
            if 'event-stream' in content_type:
                return self._read_stream(response, None, None)
            return self._read_json(response)
        finally:
            close = getattr(response, 'close', None)
            if close is not None:
                try:
                    close()
                except Exception:
                    pass

    def _read_stream(self, response, on_delta, cancel_event):
        pieces = []
        usage = {}
        model = ''
        finish_reason = ''
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise ProviderError('cancelled', 'The request was cancelled.', False)
            try:
                line = response.readline(MAX_LINE_BYTES + 1)
            except (socket.timeout, TimeoutError):
                raise ProviderError('timeout', 'The provider timed out.', True) from None
            except OSError as exc:
                if getattr(exc, 'errno', None) == errno.ETIMEDOUT:
                    raise ProviderError('timeout', 'The provider timed out.', True) from None
                raise ProviderError('connection_error',
                                    'The provider connection dropped.', True) from None
            if not line:
                break
            if len(line) > MAX_LINE_BYTES:
                raise ProviderError('invalid_response',
                                    'The provider sent an oversized stream line.', True)
            try:
                text = line.decode('utf-8').strip()
            except UnicodeDecodeError:
                raise ProviderError('invalid_response',
                                    'The provider stream was not valid UTF-8.', True) from None
            if not text or text.startswith(':'):
                continue
            if not text.startswith('data:'):
                continue
            data = text[5:].strip()
            if data == '[DONE]':
                break
            try:
                chunk = json.loads(data)
            except ValueError:
                raise ProviderError('invalid_response',
                                    'The provider sent a malformed stream chunk.', True) from None
            if not isinstance(chunk, dict):
                continue
            error = chunk.get('error')
            if isinstance(error, dict):
                message = error.get('message')
                raise ProviderError('provider_error',
                                    str(message or 'Provider error.')[:MAX_ERROR_MESSAGE_CHARS],
                                    False)
            model = str(chunk.get('model') or model)
            if isinstance(chunk.get('usage'), dict):
                usage = chunk['usage']
            choices = chunk.get('choices')
            if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                finish_reason = str(choices[0].get('finish_reason') or finish_reason)
                delta = choices[0].get('delta')
                if isinstance(delta, dict):
                    piece = delta.get('content')
                    if isinstance(piece, str) and piece:
                        pieces.append(piece)
                        if on_delta is not None:
                            on_delta(piece)
        if not pieces and not usage:
            raise ProviderError('invalid_response',
                                'The provider returned no content.', True)
        return Completion(''.join(pieces), parse_usage(usage), model, finish_reason)

    def _read_json(self, response):
        body = self._read_body(response)
        try:
            value = json.loads(body.decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            raise ProviderError('invalid_response',
                                'The provider reply was not valid JSON.', True) from None
        if not isinstance(value, dict):
            raise ProviderError('invalid_response',
                                'The provider reply was not an object.', True)
        error = value.get('error')
        if isinstance(error, dict):
            message = error.get('message') or error.get('type') or 'Provider error.'
            raise ProviderError('provider_error',
                                str(message)[:MAX_ERROR_MESSAGE_CHARS], False)
        choices = value.get('choices')
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise ProviderError('invalid_response',
                                'The provider reply has no choices.', True)
        message = choices[0].get('message')
        content = message.get('content') if isinstance(message, dict) else ''
        if not isinstance(content, str):
            raise ProviderError('invalid_response',
                                'The provider reply has no text.', True)
        return Completion(content, parse_usage(value.get('usage')),
                          str(value.get('model') or self.model),
                          str(choices[0].get('finish_reason') or ''))

    @staticmethod
    def _read_body(response):
        chunks = []
        total = 0
        while True:
            try:
                chunk = response.read(65536)
            except (socket.timeout, TimeoutError):
                raise ProviderError('timeout', 'The provider timed out.', True) from None
            except OSError:
                raise ProviderError('connection_error',
                                    'The provider connection dropped.', True) from None
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_BODY_BYTES:
                raise ProviderError('invalid_response',
                                    'The provider reply was too large.', True)
            chunks.append(chunk)
        return b''.join(chunks)
