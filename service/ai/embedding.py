"""OpenAI-compatible embeddings over urllib.

A transport only opens the connection and returns an object exposing ``read``
and ``close``; tests inject a fake transport, so no test ever needs a network.
Every failure is normalized to ``EmbeddingError(code, message, retryable,
status)`` and the reply is validated strictly before any vector is returned.
"""

from dataclasses import dataclass
import json
import math
import socket

from service.ai.budget import parse_usage
from service.ai.provider import TransportError, UrllibTransport
from service.jsonutil import dumps

DEFAULT_EMBEDDINGS_PATH = '/v1/embeddings'
MAX_TEXTS = 64
MAX_TEXT_CHARS = 8000
MAX_BODY_BYTES = 8 * 1024 * 1024
MAX_ERROR_MESSAGE_CHARS = 200


class EmbeddingError(Exception):
    def __init__(self, code, message, retryable=False, status=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = bool(retryable)
        self.status = status


@dataclass
class EmbeddingResult:
    vectors: list
    model: str
    usage: dict
    dim: int


def _status_error(status, body=b''):
    message = _error_message(body)
    if status == 402:
        return EmbeddingError('payment_required',
                              message or 'Payment is required.', False, status)
    if status in (401, 403):
        return EmbeddingError('auth_error', message or 'Authentication failed.',
                              False, status)
    if status == 404:
        return EmbeddingError('model_not_found', message or 'Model not found.',
                              False, status)
    if status == 408:
        return EmbeddingError('timeout', message or 'The provider timed out.',
                              True, status)
    if status == 429:
        return EmbeddingError('rate_limited', message or 'Rate limited.', True, status)
    if 500 <= status <= 599:
        return EmbeddingError('server_error', message or 'The provider failed.',
                              True, status)
    if 400 <= status <= 499:
        return EmbeddingError('provider_rejected',
                              message or 'The provider rejected the request.',
                              False, status)
    return EmbeddingError('provider_error', message or 'Provider error.', False, status)


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


def _translate(exc):
    if exc.kind == 'timeout':
        return EmbeddingError('timeout', 'The provider timed out.', True)
    if exc.kind == 'connection':
        return EmbeddingError('connection_error',
                              'The provider could not be reached.', True)
    if exc.kind == 'http':
        return _status_error(exc.status, exc.body)
    return EmbeddingError('provider_error', 'Provider error.', False)


def _vector(value, expected_dim):
    if type(value) is not list or not value:
        raise EmbeddingError('invalid_response',
                             'The provider returned an empty embedding.', True)
    if expected_dim and len(value) != expected_dim:
        raise EmbeddingError('dimension_mismatch',
                             'The provider returned an unexpected dimension.', False)
    numbers = []
    for item in value:
        if isinstance(item, bool) or type(item) not in (int, float):
            raise EmbeddingError('invalid_response',
                                 'The provider returned a non-numeric vector.', True)
        number = float(item)
        if not math.isfinite(number):
            raise EmbeddingError('invalid_response',
                                 'The provider returned a non-finite vector.', True)
        numbers.append(number)
    return numbers


class EmbeddingClient:
    """One embeddings endpoint; ``embed`` performs exactly one HTTP request."""

    def __init__(self, ai_settings, transport=None, model=None, path=None):
        settings = ai_settings if isinstance(ai_settings, dict) else {}
        embedding = settings.get('embedding')
        embedding = embedding if isinstance(embedding, dict) else {}
        self.base_url = str(settings.get('base_url') or 'https://api.deepseek.com').rstrip('/')
        chosen_path = path or settings.get('embeddings_path') or DEFAULT_EMBEDDINGS_PATH
        self.path = chosen_path if str(chosen_path).startswith('/') else '/' + str(chosen_path)
        self.model = str(model or embedding.get('model') or '')
        self.dimensions = int(embedding.get('dimensions') or 0)
        self.connect_timeout = max(0.001, int(settings.get('connect_timeout_ms', 5000)) / 1000.0)
        self.request_timeout = max(0.001, int(settings.get('request_timeout_ms', 30000)) / 1000.0)
        self.transport = transport if transport is not None else UrllibTransport()

    @property
    def endpoint(self):
        return self.base_url + self.path

    @staticmethod
    def _headers(key):
        return {
            'Authorization': 'Bearer ' + key,
            'Content-Type': 'application/json',
            'Accept': 'application/json',
        }

    def embed(self, texts, key=None, cancel_event=None):
        if cancel_event is not None and cancel_event.is_set():
            raise EmbeddingError('cancelled', 'The request was cancelled.', False)
        if type(key) is not str or not key:
            raise EmbeddingError('missing_key', 'No API key is configured.', False)
        inputs = self._inputs(texts)
        payload = {'model': self.model, 'input': inputs}
        try:
            response = self.transport.send(
                self.endpoint, self._headers(key), dumps(payload),
                connect_timeout=self.connect_timeout,
                read_timeout=self.request_timeout)
        except TransportError as exc:
            raise _translate(exc) from None
        except (socket.timeout, TimeoutError):
            raise EmbeddingError('timeout', 'The provider timed out.', True) from None
        except OSError:
            raise EmbeddingError('connection_error',
                                 'The provider could not be reached.', True) from None
        try:
            body = self._read_body(response)
        finally:
            close = getattr(response, 'close', None)
            if close is not None:
                try:
                    close()
                except Exception:
                    pass
        return self._parse(body, len(inputs))

    @staticmethod
    def _inputs(texts):
        if type(texts) is not list or not texts:
            raise EmbeddingError('invalid_request', 'At least one text is required.', False)
        if len(texts) > MAX_TEXTS:
            raise EmbeddingError('invalid_request', 'Too many texts in one request.', False)
        inputs = []
        for text in texts:
            if type(text) is not str or not text.strip():
                raise EmbeddingError('invalid_request',
                                     'Embedding inputs must be non-empty strings.', False)
            if len(text) > MAX_TEXT_CHARS:
                text = text[:MAX_TEXT_CHARS]
            inputs.append(text)
        return inputs

    @staticmethod
    def _read_body(response):
        chunks = []
        total = 0
        while True:
            try:
                chunk = response.read(65536)
            except (socket.timeout, TimeoutError):
                raise EmbeddingError('timeout', 'The provider timed out.', True) from None
            except OSError:
                raise EmbeddingError('connection_error',
                                     'The provider connection dropped.', True) from None
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_BODY_BYTES:
                raise EmbeddingError('invalid_response',
                                     'The provider reply was too large.', True)
            chunks.append(chunk)
        return b''.join(chunks)

    def _parse(self, body, count):
        try:
            value = json.loads(body.decode('utf-8'))
        except (ValueError, UnicodeError):
            raise EmbeddingError('invalid_response',
                                 'The provider reply was not valid JSON.', True) from None
        if not isinstance(value, dict):
            raise EmbeddingError('invalid_response',
                                 'The provider reply was not an object.', True)
        error = value.get('error')
        if isinstance(error, dict):
            message = error.get('message') or error.get('type') or 'Provider error.'
            raise EmbeddingError('provider_error',
                                 str(message)[:MAX_ERROR_MESSAGE_CHARS], False)
        data = value.get('data')
        if not isinstance(data, list) or len(data) != count:
            raise EmbeddingError('invalid_response',
                                 'The provider returned the wrong number of embeddings.', True)
        vectors = [None] * count
        for position, item in enumerate(data):
            if not isinstance(item, dict):
                raise EmbeddingError('invalid_response',
                                     'The provider returned a malformed embedding.', True)
            index = item.get('index', position)
            if isinstance(index, bool) or type(index) is not int \
                    or not 0 <= index < count or vectors[index] is not None:
                raise EmbeddingError('invalid_response',
                                     'The provider returned invalid embedding indexes.', True)
            vectors[index] = _vector(item.get('embedding'), self.dimensions)
        if any(vector is None for vector in vectors):
            raise EmbeddingError('invalid_response',
                                 'The provider reply is missing an embedding.', True)
        dim = len(vectors[0])
        if any(len(vector) != dim for vector in vectors):
            raise EmbeddingError('invalid_response',
                                 'The provider returned mixed dimensions.', True)
        return EmbeddingResult(
            vectors=vectors,
            model=str(value.get('model') or self.model),
            usage=parse_usage(value.get('usage')),
            dim=dim,
        )
