"""Transport error mapping and streaming decode.

``UrllibTransport`` is the only place the service touches the network. Its job
is to turn socket-level failures into the three codes the orchestrator branches
on — ``timeout``, ``connection``, ``http`` — because a timeout is retried and a
configuration error is not. These tests drive it with a fake urlopen, so the
mapping is checked without a real socket.
"""

import io
import socket
import threading
import urllib.error
import urllib.request

import pytest

from service.ai.provider import (OpenAICompatibleProvider, ProviderError,
                                 TransportError, UrllibTransport, _error_message,
                                 _header)


class _Response(io.BytesIO):
    """A minimal urlopen result: body bytes plus a settable read timeout."""

    def __init__(self, payload=b'', sock=None):
        super().__init__(payload)
        self.fp = type('Fp', (), {'raw': type('Raw', (), {'_sock': sock})()})()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class _Socket:
    def __init__(self):
        self.timeouts = []

    def settimeout(self, value):
        self.timeouts.append(value)


def test_a_successful_send_returns_the_response_and_sets_the_read_timeout(monkeypatch):
    sock = _Socket()
    response = _Response(b'{}', sock)
    seen = {}

    def fake_urlopen(request, timeout):
        seen['url'] = request.full_url
        seen['timeout'] = timeout
        return response

    monkeypatch.setattr(urllib.request, 'urlopen', fake_urlopen)

    result = UrllibTransport().send(
        'https://example.test/v1/chat', {'A': 'b'}, b'{}',
        connect_timeout=2.5, read_timeout=9.0)

    assert result is response
    assert seen == {'url': 'https://example.test/v1/chat', 'timeout': 2.5}
    assert sock.timeouts == [9.0], 'the read deadline is applied after connect'


def test_a_zero_connect_timeout_is_floored(monkeypatch):
    seen = {}
    monkeypatch.setattr(urllib.request, 'urlopen',
                        lambda request, timeout: seen.update(timeout=timeout) or _Response())

    UrllibTransport().send('https://x.test', {}, b'', 0, 0)

    assert seen['timeout'] == pytest.approx(0.001)


def test_an_http_error_carries_its_status_and_body(monkeypatch):
    def fake_urlopen(request, timeout):
        raise urllib.error.HTTPError(
            'https://x.test', 429, 'Too Many Requests', {}, io.BytesIO(b'{"error":"slow down"}'))

    monkeypatch.setattr(urllib.request, 'urlopen', fake_urlopen)

    with pytest.raises(TransportError) as caught:
        UrllibTransport().send('https://x.test', {}, b'', 1, 1)

    assert caught.value.kind == 'http'
    assert caught.value.status == 429
    assert b'slow down' in caught.value.body


def test_an_http_error_whose_body_cannot_be_read_still_maps_to_http(monkeypatch):
    class _Unreadable(io.BytesIO):
        def read(self, *_args):
            raise OSError('body gone')

    def fake_urlopen(request, timeout):
        raise urllib.error.HTTPError('https://x.test', 500, 'Server Error', {}, _Unreadable())

    monkeypatch.setattr(urllib.request, 'urlopen', fake_urlopen)

    with pytest.raises(TransportError) as caught:
        UrllibTransport().send('https://x.test', {}, b'', 1, 1)

    assert caught.value.kind == 'http'
    assert caught.value.status == 500
    assert caught.value.body == b''


def test_a_socket_timeout_inside_urlerror_maps_to_timeout(monkeypatch):
    def fake_urlopen(request, timeout):
        raise urllib.error.URLError(socket.timeout('timed out'))

    monkeypatch.setattr(urllib.request, 'urlopen', fake_urlopen)

    with pytest.raises(TransportError) as caught:
        UrllibTransport().send('https://x.test', {}, b'', 1, 1)

    assert caught.value.kind == 'timeout'


def test_a_dns_failure_maps_to_connection(monkeypatch):
    def fake_urlopen(request, timeout):
        raise urllib.error.URLError('Name or service not known')

    monkeypatch.setattr(urllib.request, 'urlopen', fake_urlopen)

    with pytest.raises(TransportError) as caught:
        UrllibTransport().send('https://x.test', {}, b'', 1, 1)

    assert caught.value.kind == 'connection'


def test_a_bare_socket_timeout_maps_to_timeout(monkeypatch):
    def fake_urlopen(request, timeout):
        raise socket.timeout('timed out')

    monkeypatch.setattr(urllib.request, 'urlopen', fake_urlopen)

    with pytest.raises(TransportError) as caught:
        UrllibTransport().send('https://x.test', {}, b'', 1, 1)

    assert caught.value.kind == 'timeout'


def test_a_generic_os_error_maps_to_connection(monkeypatch):
    def fake_urlopen(request, timeout):
        raise OSError('network is unreachable')

    monkeypatch.setattr(urllib.request, 'urlopen', fake_urlopen)

    with pytest.raises(TransportError) as caught:
        UrllibTransport().send('https://x.test', {}, b'', 1, 1)

    assert caught.value.kind == 'connection'


def test_a_response_without_a_socket_ignores_the_timeout():
    response = _Response(b'{}', None)

    UrllibTransport._set_read_timeout(response, 5.0)  # must not raise


def test_a_socket_that_refuses_the_timeout_is_ignored():
    class _Refusing:
        def settimeout(self, _value):
            raise OSError('already closed')

    _UrllibTransportHelper = UrllibTransport()
    response = _Response(b'{}', _Refusing())
    _UrllibTransportHelper._set_read_timeout(response, 5.0)  # must not raise


# -- header lookup ---------------------------------------------------------


class _Headers:
    def __init__(self, values):
        self.values = values

    def get(self, name):
        return self.values.get(name)


def test_header_lookup_tries_the_given_name_then_the_lowercase_one():
    class _CaseSensitive:
        headers = None

        def __init__(self, values):
            self.headers = self

        def get(self, name):
            return self.values.get(name)

    names = {}

    class _Recorder:
        headers = None

        def __init__(self):
            self.headers = self

        def get(self, name):
            names.setdefault('asked', []).append(name)
            return {'content-type': 'application/json'}.get(name)

    assert _header(_Recorder(), 'Content-Type') == 'application/json'
    assert names['asked'] == ['Content-Type', 'content-type'], (
        'the exact name is tried first, then the lowercase form')


def test_header_lookup_returns_empty_for_a_missing_or_odd_header_map():
    assert _header(_Headers({}), 'x') == ''
    assert _header(object(), 'x') == ''
    assert _header(_Headers({'x': 5}), 'x') == ''


# -- provider error mapping ------------------------------------------------


class _NoCall:
    def __init__(self):
        self.calls = 0

    def send(self, *_args, **_kwargs):
        self.calls += 1
        raise AssertionError('the transport must not be reached')


class _Failing:
    def __init__(self, error):
        self.error = error

    def send(self, *_args, **_kwargs):
        raise self.error


class _Raising:
    def __init__(self, error):
        self.error = error

    def send(self, *_args, **_kwargs):
        raise self.error


def test_an_empty_base_url_falls_back_to_the_shipped_default():
    provider = OpenAICompatibleProvider({'base_url': '', 'model': 'm'})

    assert provider.endpoint == 'https://api.deepseek.com/v1/chat/completions'


def test_a_chat_path_without_a_leading_slash_is_normalised():
    provider = OpenAICompatibleProvider(
        {'base_url': 'https://x.test/', 'chat_path': 'v1/chat/completions'})

    assert provider.endpoint == 'https://x.test/v1/chat/completions'


def test_a_missing_api_key_is_reported_before_the_transport_is_reached():
    transport = _NoCall()
    provider = OpenAICompatibleProvider(
        {'base_url': 'https://x.test', 'model': 'm'}, transport=transport)

    with pytest.raises(ProviderError) as caught:
        provider.stream_chat([{'role': 'user', 'content': 'hi'}], '')

    assert caught.value.code == 'missing_key'
    assert caught.value.retryable is False
    assert transport.calls == 0


@pytest.mark.parametrize('status,code,retryable', [
    (402, 'payment_required', False),
    (401, 'auth_error', False),
    (403, 'auth_error', False),
    (404, 'model_not_found', False),
    (408, 'timeout', True),
    (429, 'rate_limited', True),
    (500, 'server_error', True),
    (503, 'server_error', True),
    (400, 'provider_rejected', False),
    (418, 'provider_rejected', False),
    (302, 'provider_error', False),
])
def test_an_http_status_maps_to_the_documented_error_code(status, code, retryable):
    provider = OpenAICompatibleProvider(
        {'base_url': 'https://x.test', 'model': 'm'},
        transport=_Failing(TransportError('http', 'HTTP %d' % status, status, b'')))

    with pytest.raises(ProviderError) as caught:
        provider.stream_chat([{'role': 'user', 'content': 'hi'}], 'k')

    assert (caught.value.code, caught.value.retryable) == (code, retryable)


@pytest.mark.parametrize('kind,code', [
    ('timeout', 'timeout'),
    ('connection', 'connection_error'),
    ('mystery', 'provider_error'),
])
def test_a_transport_kind_maps_to_its_error_code(kind, code):
    provider = OpenAICompatibleProvider(
        {'base_url': 'https://x.test', 'model': 'm'},
        transport=_Failing(TransportError(kind, 'failed')))

    with pytest.raises(ProviderError) as caught:
        provider.stream_chat([{'role': 'user', 'content': 'hi'}], 'k')

    assert caught.value.code == code
    assert caught.value.retryable == (kind in ('timeout', 'connection'))


def test_a_cancelled_request_never_reaches_the_transport():
    transport = _NoCall()
    provider = OpenAICompatibleProvider(
        {'base_url': 'https://x.test', 'model': 'm'}, transport=transport)
    cancel = threading.Event()
    cancel.set()

    with pytest.raises(ProviderError) as caught:
        provider.stream_chat([{'role': 'user', 'content': 'hi'}], 'k',
                             cancel_event=cancel)

    assert caught.value.code == 'cancelled'
    assert transport.calls == 0


def test_a_socket_timeout_escaping_the_transport_is_retryable():
    provider = OpenAICompatibleProvider(
        {'base_url': 'https://x.test', 'model': 'm'},
        transport=_Raising(socket.timeout('timed out')))

    with pytest.raises(ProviderError) as caught:
        provider.stream_chat([{'role': 'user', 'content': 'hi'}], 'k')

    assert (caught.value.code, caught.value.retryable) == ('timeout', True)


def test_an_os_error_escaping_the_transport_is_retryable():
    provider = OpenAICompatibleProvider(
        {'base_url': 'https://x.test', 'model': 'm'},
        transport=_Raising(OSError('unreachable')))

    with pytest.raises(ProviderError) as caught:
        provider.stream_chat([{'role': 'user', 'content': 'hi'}], 'k')

    assert (caught.value.code, caught.value.retryable) == ('connection_error', True)


# -- error message extraction ----------------------------------------------


@pytest.mark.parametrize('body,expected', [
    (b'{"error": {"message": "slow down"}}', 'slow down'),
    (b'{"error": {"type": "rate_limit"}}', 'rate_limit'),
    (b'{"error": "plain string"}', 'plain string'),
    (b'{"message": "top level"}', 'top level'),
    (b'not json', ''),
    (b'[]', ''),
    (b'{"error": {"message": 5}}', ''),
    (b'', ''),
])
def test_the_provider_error_message_is_extracted_defensively(body, expected):
    assert _error_message(body) == expected


