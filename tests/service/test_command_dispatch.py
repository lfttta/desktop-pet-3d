"""Command dispatch edge cases: the null context, reserved prefixes, errors.

``dispatch`` without a runtime context is how tests and one-off calls reach a
handler, so the null context's refusals are part of the contract. The reserved
prefix table decides whether an unregistered command answers ``not_available``
or ``unknown_command``, which the app uses to decide between "this build does
not have it" and "that is a typo".
"""

import pytest

from service.commands import _NullContext, dispatch
from service.protocol import ProtocolError
from service.settings import SettingsStore


@pytest.fixture()
def store(tmp_path):
    return SettingsStore(tmp_path)


def _request(command, payload=None, request_id='r1'):
    return {'type': command, 'id': request_id, 'payload': payload or {}}


# -- null context ----------------------------------------------------------


def test_the_null_context_refuses_every_transport_operation():
    context = _NullContext()

    with pytest.raises(RuntimeError, match='transport context'):
        context.emit('result', {})
    with pytest.raises(RuntimeError, match='transport context'):
        context.post('result', {})
    with pytest.raises(RuntimeError, match='tick registration'):
        context.register_tick(lambda: None)


def test_the_null_context_records_log_lines_without_a_buffer():
    context = _NullContext()

    context.log.record('error', 'module', 'code', 'message')
    assert context.log.tail() == []
    assert context.log.tail(10) == []


# -- reserved prefixes -----------------------------------------------------


@pytest.mark.parametrize('command', [
    'voice.synthesize',
    'tts.cancel',
    'action.perform',
    'personality.get',
    'memory.fuzzy.search',
    'user.profile',
])
def test_a_reserved_prefix_answers_not_available(store, command):
    with pytest.raises(ProtocolError) as caught:
        dispatch(_request(command), store)

    assert caught.value.code == 'not_available'


def test_an_unrelated_unknown_command_is_reported_as_unknown(store):
    with pytest.raises(ProtocolError) as caught:
        dispatch(_request('nonsense.command'), store)

    assert caught.value.code == 'unknown_command'


def test_the_reserved_table_is_empty_so_every_reserved_name_is_a_prefix(store):
    from service.commands import RESERVED_COMMANDS

    assert RESERVED_COMMANDS == frozenset(), (
        'a name in RESERVED_COMMANDS would shadow a real handler')


# -- hello -----------------------------------------------------------------


def test_hello_rejects_a_missing_protocol_version(store):
    with pytest.raises(ProtocolError) as caught:
        dispatch(_request('hello', {}), store)

    assert caught.value.code == 'invalid_request'


def test_hello_rejects_a_boolean_protocol_version(store):
    with pytest.raises(ProtocolError) as caught:
        dispatch(_request('hello', {'protocol_version': True}), store)

    assert caught.value.code == 'invalid_request'


def test_hello_rejects_another_protocol_version(store):
    with pytest.raises(ProtocolError) as caught:
        dispatch(_request('hello', {'protocol_version': 2}), store)

    assert caught.value.code == 'unsupported_version'


# -- settings --------------------------------------------------------------


def test_settings_update_surfaces_a_validation_error_as_invalid_settings(store):
    with pytest.raises(ProtocolError) as caught:
        dispatch(_request('settings.update', {'scale': 99}), store)

    assert caught.value.code == 'invalid_settings'
    assert store.settings['scale'] == 1.0, 'a rejected patch must not be applied'


def test_settings_update_surfaces_a_write_failure_as_save_failed(store, monkeypatch):
    def explode(*_args, **_kwargs):
        raise OSError('disk full')

    monkeypatch.setattr(store, 'update', explode)

    with pytest.raises(ProtocolError) as caught:
        dispatch(_request('settings.update', {'scale': 1.2}), store)

    assert caught.value.code == 'save_failed'


def test_settings_get_returns_the_current_document(store):
    event, payload = dispatch(_request('settings.get'), store)

    assert event == 'result'
    assert payload['settings'] == store.settings


def test_shutdown_returns_an_empty_result(store):
    assert dispatch(_request('shutdown'), store) == ('result', {})
