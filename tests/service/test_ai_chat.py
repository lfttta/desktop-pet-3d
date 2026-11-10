"""Chat and AI pipeline tests using fake transports; no real network calls."""

import io
import json
import sys
import time

import pytest

from service.ai.budget import BudgetLedger, estimate_cost, parse_usage
from service.ai.circuit import CircuitBreaker
from service.ai.orchestrator import ChatOrchestrator, SAFE_FALLBACK
from service.ai.prompt import MAX_PROMPT_CHARS, build_messages, search_memories
from service.ai.provider import OpenAICompatibleProvider, ProviderError, TransportError
from service.ai.router import Router
from service.ai.schema import SchemaError, parse_structured
from service.commands import dispatch
from service.database import Database
from service.protocol import ProtocolError
from service.runtime import BoundedLog, Runtime
from service.secrets import SecretStore, SecretsError
from service.settings import SettingsStore

FAKE_KEY = 'test-key-not-real'


def sse(payload):
    return ('data: ' + json.dumps(payload, ensure_ascii=False) + '\n').encode('utf-8')


def content_lines(pieces, usage=None, delay=0.0):
    lines = [sse({'model': 'deepseek-chat',
                  'choices': [{'delta': {'content': piece}}]}) for piece in pieces]
    if usage is not None:
        lines.append(sse({'model': 'deepseek-chat', 'choices': [], 'usage': usage}))
    lines.append(b'data: [DONE]\n')
    return FakeResponse(lines, delay=delay)


def stream_for(payload, usage=None, chunk_size=7, delay=0.0):
    raw = json.dumps(payload, ensure_ascii=False)
    pieces = [raw[i:i + chunk_size] for i in range(0, len(raw), chunk_size)]
    return content_lines(pieces, usage=usage, delay=delay)


def structured(text='你好呀', action='wave', candidates=None, suggestion=None,
               confidence=0.9):
    return {
        'text': text,
        'speak': text,
        'action': action,
        'memory_candidates': candidates or [],
        'state_suggestion': suggestion or {},
        'confidence': confidence,
    }


class FakeResponse:
    def __init__(self, lines=(), body=b'', headers=None, delay=0.0):
        self._lines = list(lines)
        self._body = body
        self.headers = headers or {'Content-Type': 'text/event-stream'}
        self.delay = delay
        self.closed = False

    def readline(self, size=-1):
        if self.delay:
            time.sleep(self.delay)
        if not self._lines:
            return b''
        return self._lines.pop(0)

    def read(self, size=-1):
        if size is None or size < 0:
            chunk, self._body = self._body, b''
            return chunk
        chunk, self._body = self._body[:size], self._body[size:]
        return chunk

    def close(self):
        self.closed = True


class ScriptedTransport:
    def __init__(self, scripts):
        self.scripts = list(scripts)
        self.requests = []
        self.calls = 0

    def send(self, url, headers, body, connect_timeout, read_timeout):
        self.requests.append({
            'url': url,
            'headers': dict(headers),
            'body': json.loads(body.decode('utf-8')),
        })
        index = min(self.calls, len(self.scripts) - 1)
        item = self.scripts[index]
        self.calls += 1
        if isinstance(item, Exception):
            raise item
        return item


def json_response(content, prompt=3, completion=2):
    body = json.dumps({
        'model': 'deepseek-chat',
        'choices': [{'message': {'role': 'assistant', 'content': content},
                     'finish_reason': 'stop'}],
        'usage': {'prompt_tokens': prompt, 'completion_tokens': completion,
                  'total_tokens': prompt + completion},
    }, ensure_ascii=False).encode('utf-8')
    return FakeResponse(body=body, headers={'Content-Type': 'application/json'})


class FakeContext:
    def __init__(self, database):
        self.database = database
        self.runtime = None
        self.events = []
        self.hooks = []

    def emit(self, event, payload):
        self.events.append((event, payload))

    def post(self, event, payload):
        self.events.append((event, payload))

    def register_tick(self, hook):
        self.hooks.append(hook)

    def terminal(self):
        for event, payload in reversed(self.events):
            if event in ('chat.done', 'chat.error'):
                return event, payload
        return None


def make_env(tmp_path, transport, **kwargs):
    store = SettingsStore(tmp_path)
    database = Database(tmp_path / 'pet.db')
    store.ai_transport = transport
    store.ai_key_provider = lambda: FAKE_KEY
    orchestrator = ChatOrchestrator(store, database, transport=transport,
                                    retry_delay=0, **kwargs)
    store._chat_orchestrator = orchestrator
    return store, database, orchestrator, FakeContext(database)


def pump(orchestrator, ctx, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        orchestrator.poll()
        terminal = ctx.terminal()
        if terminal is not None:
            return terminal
        time.sleep(0.005)
    raise AssertionError('no terminal chat event: %r' % (ctx.events,))


def send(store, ctx, text, request_id='req-1'):
    return dispatch({'type': 'chat.send', 'id': request_id,
                     'payload': {'text': text}}, store, ctx)


class FrameThenIdleInput:
    def __init__(self, frame, idle=0.4):
        self.frame = frame
        self.idle = idle
        self.sent = False

    def read1(self, size):
        if not self.sent:
            self.sent = True
            return self.frame
        time.sleep(self.idle)
        return b''

    def read(self, size):
        return self.read1(size)


def rows(database, sql, params=()):
    return database.connection.execute(sql, params).fetchall()


# Provider contracts


def test_provider_streams_sse_and_usage():
    transport = ScriptedTransport([
        content_lines(['你', '好', '呀'], usage={'prompt_tokens': 10,
                                                 'completion_tokens': 5,
                                                 'total_tokens': 15})])
    provider = OpenAICompatibleProvider({'base_url': 'https://example.test'}, transport)
    deltas = []
    completion = provider.stream_chat([{'role': 'user', 'content': 'hi'}],
                                      FAKE_KEY, on_delta=deltas.append)
    assert deltas == ['你', '好', '呀']
    assert completion.text == '你好呀'
    assert completion.usage == {'prompt_tokens': 10, 'completion_tokens': 5,
                                'total_tokens': 15}
    request = transport.requests[0]
    assert request['url'] == 'https://example.test/v1/chat/completions'
    assert request['headers']['Authorization'] == 'Bearer ' + FAKE_KEY
    assert request['body']['stream'] is True


def test_provider_402_is_not_retryable():
    transport = ScriptedTransport([
        TransportError('http', 'HTTP 402', status=402,
                       body=b'{"error":{"message":"Insufficient Balance"}}')])
    provider = OpenAICompatibleProvider({}, transport)
    with pytest.raises(ProviderError) as caught:
        provider.stream_chat([{'role': 'user', 'content': 'hi'}], FAKE_KEY)
    assert caught.value.code == 'payment_required'
    assert caught.value.retryable is False
    assert caught.value.status == 402
    assert 'Insufficient' in caught.value.message


def test_provider_429_is_retryable():
    transport = ScriptedTransport([
        TransportError('http', 'HTTP 429', status=429,
                       body=b'{"error":{"message":"rate limit"}}')])
    provider = OpenAICompatibleProvider({}, transport)
    with pytest.raises(ProviderError) as caught:
        provider.stream_chat([{'role': 'user', 'content': 'hi'}], FAKE_KEY)
    assert caught.value.code == 'rate_limited'
    assert caught.value.retryable is True


def test_provider_timeout_and_connection_are_distinct():
    for kind, code in (('timeout', 'timeout'),
                       ('connection', 'connection_error')):
        provider = OpenAICompatibleProvider(
            {}, ScriptedTransport([TransportError(kind, kind)]))
        with pytest.raises(ProviderError) as caught:
            provider.stream_chat([{'role': 'user', 'content': 'hi'}], FAKE_KEY)
        assert caught.value.code == code
        assert caught.value.retryable is True


def test_provider_reads_non_stream_json_reply():
    transport = ScriptedTransport([json_response('pong')])
    provider = OpenAICompatibleProvider({}, transport)
    completion = provider.probe(FAKE_KEY)
    assert completion.text == 'pong'
    assert completion.usage['prompt_tokens'] == 3
    assert transport.requests[0]['body']['stream'] is False


# Structured schema


def test_schema_accepts_known_action_and_rejects_unknown_action():
    parsed = parse_structured(json.dumps(structured(action='wave')))
    assert parsed['action'] == 'wave'
    with pytest.raises(SchemaError) as caught:
        parse_structured(json.dumps(structured(action='dance')))
    assert caught.value.code == 'unknown_action'


def test_schema_accepts_every_chat_action():
    from service.ai.actions import allowed

    for action in allowed():
        parsed = parse_structured(json.dumps(structured(action=action)))
        assert parsed['action'] == action


@pytest.mark.parametrize('action', [
    'pickup',     # hold-mode: a reply would latch the pet mid-pose
    'fall',       # hold-mode
    'running',    # loop-mode: never completes
    'sleeping_idle',
    'turn',       # rewrites the body yaw
    'recover',    # owned by the landing path
    'male_sitting_pose',
])
def test_schema_rejects_actions_the_chat_path_cannot_drive(action):
    with pytest.raises(SchemaError) as caught:
        parse_structured(json.dumps(structured(action=action)))
    assert caught.value.code == 'unknown_action'


def test_prompt_lists_the_whole_chat_vocabulary():
    from service.ai.actions import prompt_list
    from service.ai.prompt import OUTPUT_RULES

    for action in prompt_list().split('/'):
        assert action in OUTPUT_RULES, f'{action} missing from output rules'
    assert 'none' in OUTPUT_RULES


@pytest.mark.parametrize('mutate, code', [
    (lambda item: item.pop('text'), 'missing_field'),
    (lambda item: item.update(confidence=2.0), 'out_of_range'),
    (lambda item: item.update(confidence='high'), 'invalid_type'),
    (lambda item: item.update(memory_candidates='one'), 'invalid_type'),
    (lambda item: item.update(state_suggestion={'Mood': 1}), 'invalid_type'),
    (lambda item: item.update(state_suggestion={'mood': 5}), 'out_of_range'),
])
def test_schema_rejects_missing_type_and_range_errors(mutate, code):
    item = structured()
    mutate(item)
    with pytest.raises(SchemaError) as caught:
        parse_structured(json.dumps(item))
    assert caught.value.code == code


def test_schema_rejects_non_json():
    with pytest.raises(SchemaError) as caught:
        parse_structured('not json')
    assert caught.value.code == 'invalid_json'


# Router and prompt


def test_router_handles_local_commands_only():
    router = Router()
    assert router.local_model is False
    assert router.route('讲个笑话') is None
    wave = router.route('挥挥手')
    assert wave.action == 'wave'
    remember = router.route('记住：我喜欢下雨天')
    assert remember.memory_text == '我喜欢下雨天'
    assert router.route('你好').name == 'hello'
    assert router.route('你好呀。').name == 'hello'
    assert router.route('你好，用一句话介绍你自己。') is None
    assert router.route('忘掉那件事').name == 'forget'


def test_prompt_stays_within_budget_and_skips_full_history(tmp_path):
    store = SettingsStore(tmp_path)
    database = Database(tmp_path / 'pet.db')
    try:
        with database.transaction() as connection:
            connection.execute("INSERT INTO conversations(id) VALUES('c-1')")
            for index in range(20):
                connection.execute(
                    'INSERT INTO messages(id, conversation_id, role, content) '
                    'VALUES(?,?,?,?)',
                    ('m-%d' % index, 'c-1', 'user' if index % 2 == 0 else 'assistant',
                     '消息%02d' % index + '内容' * 120))
            connection.execute(
                "INSERT INTO memories(id, content, importance) VALUES('mem-1', ?, 0.9)",
                ('用户喜欢下雨天' * 30,))
        messages = build_messages(database, 'c-1', '下雨天', store.settings)
        measured = sum(len(item['content']) + len(item['role']) + 4
                       for item in messages)
        assert measured <= MAX_PROMPT_CHARS
        assert messages[0]['role'] == 'system'
        assert messages[-1] == {'role': 'user', 'content': '下雨天'}
        assert len(messages) <= 18
        assert '消息19' in messages[-2]['content']
        assert '消息00' not in ''.join(item['content'] for item in messages)
        assert '用户喜欢下雨天' in messages[0]['content']
    finally:
        database.close()


def test_memory_search_falls_back_to_like(tmp_path):
    database = Database(tmp_path / 'pet.db', enable_fts=False)
    try:
        with database.transaction() as connection:
            connection.execute(
                "INSERT INTO memories(id, content) VALUES('m-1', '喜欢下雨天')")
        assert search_memories(database, '下雨天', 5, 100) == ['喜欢下雨天']
    finally:
        database.close()


def test_prompt_memory_fallback_filters_by_subject(tmp_path, monkeypatch):
    from service.memory.store import MemoryStore
    database = Database(tmp_path / 'pet.db')
    try:
        store = MemoryStore(database)
        monkeypatch.setattr('service.ai.prompt._memory_search_hits',
                            lambda *args, **kwargs: [])
        for memory_id, subject, content in (
                ('m-owner', 'owner', '我喜欢下雨天'),
                ('m-other', 'other', '别人的下雨天秘密')):
            with database.transaction() as connection:
                store.insert_memory(connection, {
                    'id': memory_id, 'content': content, 'created_at': '2026-11-04T00:00:00Z',
                    'subject_user_id': subject, 'category': 'general',
                    'importance': 0.5, 'source_type': 'manual',
                    'updated_at': '2026-11-04T00:00:00Z', 'status': 'active',
                    'body_hash': '', 'mention_count': 1, 'session_count': 1,
                    'recall_count': 0, 'confidence': 0.5, 'strength': 0.0,
                    'last_reinforced_at': '2026-11-04T00:00:00Z',
                    'dedupe_key': content,
                })
            store.index_memory(memory_id, content)
        assert search_memories(database, '下雨天', 5, 100) == ['我喜欢下雨天']
    finally:
        database.close()


# Chat flow


def test_chat_send_returns_accepted_before_network(tmp_path):
    transport = ScriptedTransport([
        stream_for(structured(text='慢慢说'), chunk_size=1000, delay=0.5)])
    store, database, orchestrator, ctx = make_env(tmp_path, transport)
    try:
        started = time.perf_counter()
        event, payload = send(store, ctx, '讲个笑话吧')
        elapsed = time.perf_counter() - started
        assert event == 'accepted'
        assert payload['request_id'].startswith('chat-')
        assert elapsed < 0.05
        assert ctx.terminal() is None
        terminal, done = pump(orchestrator, ctx)
        assert terminal == 'chat.done'
        assert done['message']['content'] == '慢慢说'
    finally:
        database.close()


def test_chat_stream_persists_and_reports_pending_memory(tmp_path):
    store, database, orchestrator, ctx = make_env(tmp_path, None)
    transport = ScriptedTransport([
        stream_for(structured(text='今天也要开心呀', action='wave',
                              candidates=['喜欢下雨天'],
                              suggestion={'mood': 0.2}),
                   usage={'prompt_tokens': 10, 'completion_tokens': 5,
                          'total_tokens': 15})])
    store.ai_transport = transport
    orchestrator.transport = transport
    store.update({'ai': {'input_price_per_million': 0.5,
                         'output_price_per_million': 1.0}})
    try:
        send(store, ctx, '讲个笑话吧')
        terminal, done = pump(orchestrator, ctx)
        assert terminal == 'chat.done'
        deltas = ''.join(payload['text'] for event, payload in ctx.events
                         if event == 'chat.delta')
        assert deltas == '今天也要开心呀'
        assert done['message']['action'] == 'wave'
        assert done['usage']['prompt_tokens'] == 10
        assert done['usage']['completion_tokens'] == 5
        assert done['usage']['cost'] == pytest.approx(0.00001)
        assert done['memory_ops'] == [{'operation': 'propose',
                                       'status': 'pending',
                                       'content': '喜欢下雨天'}]
        assert done['state_delta']['applied'] is True
        assert done['state_delta']['suggestion'] == {'mood': 0.2}
        assert 'state' in done['state_delta']
        stored = rows(database, 'SELECT role, content FROM messages ORDER BY rowid')
        assert stored == [('user', '讲个笑话吧'),
                          ('assistant', '今天也要开心呀')]
        ledger = rows(database, 'SELECT prompt_tokens, completion_tokens, cost '
                                'FROM usage_ledger')
        assert ledger == [(10, 5, pytest.approx(0.00001))]
    finally:
        database.close()


def test_local_remember_persists_the_memory(tmp_path):
    store, database, orchestrator, ctx = make_env(tmp_path, None)
    try:
        send(store, ctx, '记住：我喜欢下雨天')
        terminal, done = pump(orchestrator, ctx)
        assert terminal == 'chat.done'
        assert done['message']['content'] == '我记住了。'
        assert done['memory_ops'][0]['status'] == 'committed'
        assert done['memory_ops'][0]['memory_id']
        assert rows(database, "SELECT content FROM memories WHERE status='active'") == [
            ('我喜欢下雨天',)]
    finally:
        database.close()


def test_auto_capture_persists_model_memory_candidates(tmp_path):
    transport = ScriptedTransport([
        stream_for(structured(text='记住了', candidates=['喜欢下雨天']))])
    store, database, orchestrator, ctx = make_env(tmp_path, transport)
    try:
        store.update({'memory': {'auto_capture': True}})
        send(store, ctx, '讲个笑话吧')
        terminal, done = pump(orchestrator, ctx)
        assert terminal == 'chat.done'
        assert done['memory_ops'][0]['status'] == 'committed'
        assert done['memory_ops'][0]['memory_id']
        assert rows(database, "SELECT content FROM memories WHERE status='active'") == [
            ('喜欢下雨天',)]
    finally:
        database.close()


def test_expression_style_reaches_the_prompt_and_reply(tmp_path):
    transport = ScriptedTransport([stream_for(structured(text='好的，今天下雨了。'))])
    store, database, orchestrator, ctx = make_env(tmp_path, transport)
    try:
        messages = build_messages(database, '', '你好', store.settings)
        assert any('表达风格' in item['content'] for item in messages
                   if item['role'] == 'system')
        send(store, ctx, '讲个笑话吧')
        terminal, done = pump(orchestrator, ctx)
        assert terminal == 'chat.done'
        assert done['message']['content'] == '今天下雨了。'
    finally:
        database.close()


def test_privacy_log_prompts_controls_prompt_logging(tmp_path):
    class _Log:
        def __init__(self):
            self.lines = []

        def write(self, message):
            self.lines.append(message)

    store, database, orchestrator, ctx = make_env(
        tmp_path, ScriptedTransport([stream_for(structured(text='好的'))]))
    log = _Log()
    store.log = log
    try:
        store.update({'privacy': {'log_prompts': True}})
        send(store, ctx, '讲个笑话吧')
        pump(orchestrator, ctx)
        assert any('chat prompt' in line and '讲个笑话吧' in line for line in log.lines)
        logged = len(log.lines)
        store.update({'privacy': {'log_prompts': False}})
        send(store, ctx, '再说一句')
        pump(orchestrator, ctx)
        assert len(log.lines) == logged
    finally:
        database.close()


def test_chat_history_filters_by_subject_user_id(tmp_path):
    store, database, orchestrator, ctx = make_env(tmp_path, None)
    try:
        with database.transaction() as connection:
            connection.execute(
                "INSERT INTO conversations(id, title, subject_user_id) "
                "VALUES('c-owner', 't', 'owner')")
            connection.execute(
                "INSERT INTO conversations(id, title, subject_user_id) "
                "VALUES('c-other', 't', 'other')")
            connection.execute(
                'INSERT INTO messages(id, conversation_id, role, content, subject_user_id) '
                "VALUES('m-1', 'c-owner', 'user', '属于我', 'owner')")
            connection.execute(
                'INSERT INTO messages(id, conversation_id, role, content, subject_user_id) '
                "VALUES('m-2', 'c-owner', 'user', '别人的', 'other')")
            connection.execute(
                'INSERT INTO messages(id, conversation_id, role, content, subject_user_id) '
                "VALUES('m-3', 'c-other', 'user', '别人的最新', 'other')")
        event, payload = dispatch({'type': 'chat.history', 'id': 'h',
                                   'payload': {}}, store, ctx)
        assert event == 'result'
        assert payload['conversation_id'] == 'c-owner'
        assert [item['content'] for item in payload['messages']] == ['属于我']
    finally:
        database.close()


def test_conversations_and_messages_carry_subject_user_id(tmp_path):
    database = Database(tmp_path / 'pet.db')
    try:
        for table in ('conversations', 'messages'):
            columns = {row[1] for row in
                       database.connection.execute(f'PRAGMA table_info({table})')}
            assert 'subject_user_id' in columns
    finally:
        database.close()


def test_chat_402_returns_error_without_retry(tmp_path):
    error = TransportError('http', 'HTTP 402', status=402,
                           body=b'{"error":{"message":"Insufficient Balance"}}')
    store, database, orchestrator, ctx = make_env(
        tmp_path, ScriptedTransport([error]))
    try:
        send(store, ctx, '讲个笑话吧')
        terminal, payload = pump(orchestrator, ctx)
        assert terminal == 'chat.error'
        assert payload['code'] == 'payment_required'
        assert payload['retryable'] is False
        assert orchestrator.transport.calls == 1
        assert rows(database, "SELECT role FROM messages") == [('user',)]
    finally:
        database.close()


def test_chat_429_retries_once_then_errors(tmp_path):
    error = TransportError('http', 'HTTP 429', status=429,
                           body=b'{"error":{"message":"rate limit"}}')
    store, database, orchestrator, ctx = make_env(
        tmp_path, ScriptedTransport([error]))
    try:
        send(store, ctx, '讲个笑话吧')
        terminal, payload = pump(orchestrator, ctx)
        assert terminal == 'chat.error'
        assert payload['code'] == 'rate_limited'
        assert payload['retryable'] is True
        assert orchestrator.transport.calls == 2
        assert rows(database, "SELECT role FROM messages") == [('user',)]
    finally:
        database.close()


def test_chat_timeout_uses_configured_retries(tmp_path):
    store, database, orchestrator, ctx = make_env(
        tmp_path, ScriptedTransport([TransportError('timeout', 'timed out')]))
    try:
        send(store, ctx, '讲个笑话吧')
        terminal, payload = pump(orchestrator, ctx)
        assert terminal == 'chat.error'
        assert payload['code'] == 'timeout'
        assert orchestrator.transport.calls == 2
    finally:
        database.close()


def test_chat_cancel_stops_stream_and_keeps_no_assistant_text(tmp_path):
    payload = structured(text='这是一段很长的回复' * 6)
    transport = ScriptedTransport([stream_for(payload, chunk_size=2, delay=0.02)])
    store, database, orchestrator, ctx = make_env(tmp_path, transport)
    try:
        send(store, ctx, '讲个笑话吧')
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            orchestrator.poll()
            if any(event == 'chat.delta' for event, _ in ctx.events):
                break
            time.sleep(0.005)
        # The accepted request id is the only active orchestration entry.
        active = list(orchestrator._active)
        assert active
        event, payload = dispatch({'type': 'chat.cancel', 'id': 'cancel',
                                   'payload': {'request_id': active[0]}},
                                  store, ctx)
        cancel_index = len(ctx.events)
        assert (event, payload) == ('cancelled', {})
        terminal, error = pump(orchestrator, ctx)
        assert terminal == 'chat.error'
        assert error['code'] == 'cancelled'
        assert error['retryable'] is False
        assert not any(item == 'chat.delta' for item, _ in ctx.events[cancel_index:])
        assert rows(database, "SELECT role FROM messages") == [('user',)]
        assert orchestrator.pending_count() == 0
    finally:
        database.close()


def test_chat_invalid_json_is_repaired_once(tmp_path):
    transport = ScriptedTransport([
        content_lines(['not json at all']),
        stream_for(structured(text='修好了')),
    ])
    store, database, orchestrator, ctx = make_env(tmp_path, transport)
    try:
        send(store, ctx, '讲个笑话吧')
        terminal, done = pump(orchestrator, ctx)
        assert terminal == 'chat.done'
        assert done['message']['content'] == '修好了'
        assert orchestrator.transport.calls == 2
        repair_body = transport.requests[1]['body']['messages']
        assert any('invalid' in item['content'] for item in repair_body)
    finally:
        database.close()


def test_chat_invalid_twice_degrades_to_safe_reply(tmp_path):
    transport = ScriptedTransport([
        content_lines(['not json']),
        content_lines(['still not json']),
    ])
    store, database, orchestrator, ctx = make_env(tmp_path, transport)
    try:
        send(store, ctx, '讲个笑话吧')
        terminal, done = pump(orchestrator, ctx)
        assert terminal == 'chat.done'
        assert done['usage']['degraded'] is True
        assert done['message']['content'] == SAFE_FALLBACK
        assert orchestrator.transport.calls == 2
    finally:
        database.close()


def test_chat_circuit_opens_and_recovers(tmp_path):
    now = [1000.0]
    circuit = CircuitBreaker(threshold=2, cooldown_seconds=10.0,
                             clock=lambda: now[0])
    error = TransportError('timeout', 'timed out')
    transport = ScriptedTransport([error, error, stream_for(structured(text='恢复了'))])
    store, database, orchestrator, ctx = make_env(tmp_path, transport,
                                                  circuit=circuit)
    try:
        store.update({'ai': {'max_retries': 0}})
        for _ in range(2):
            send(store, ctx, '讲个笑话吧')
            terminal, payload = pump(orchestrator, ctx)
            assert terminal == 'chat.error'
            assert payload['code'] == 'timeout'
        assert transport.calls == 2
        assert circuit.is_open() is True

        send(store, ctx, '讲个笑话吧')
        terminal, payload = pump(orchestrator, ctx)
        assert terminal == 'chat.error'
        assert payload['code'] == 'circuit_open'
        assert transport.calls == 2

        now[0] += 11.0
        send(store, ctx, '讲个笑话吧')
        terminal, done = pump(orchestrator, ctx)
        assert terminal == 'chat.done'
        assert done['message']['content'] == '恢复了'
        assert transport.calls == 3
        assert circuit.is_open() is False
    finally:
        database.close()


def test_budget_warning_is_reported(tmp_path):
    store, database, orchestrator, ctx = make_env(
        tmp_path, ScriptedTransport([stream_for(structured(text='好的'))]))
    try:
        store.update({'ai': {'daily_token_budget': 20000}})
        with database.transaction() as connection:
            connection.execute(
                'INSERT INTO usage_ledger(prompt_tokens, completion_tokens, total_tokens) '
                'VALUES(12000, 4000, 16000)')
        send(store, ctx, '讲个笑话吧')
        terminal, done = pump(orchestrator, ctx)
        assert terminal == 'chat.done'
        assert done['usage']['budget_warning'] is True
        assert orchestrator.transport.calls == 1
    finally:
        database.close()


def test_budget_reservation_blocks_a_request_that_could_exceed_the_cap(tmp_path):
    transport = ScriptedTransport([stream_for(structured(text='不该出现'))])
    store, database, orchestrator, ctx = make_env(tmp_path, transport)
    try:
        store.update({'ai': {'daily_token_budget': 10}})
        with database.transaction() as connection:
            connection.execute(
                'INSERT INTO usage_ledger(prompt_tokens, completion_tokens, total_tokens) '
                'VALUES(5, 4, 9)')
        send(store, ctx, '讲个笑话吧')
        terminal, done = pump(orchestrator, ctx)
        assert terminal == 'chat.done'
        assert done['usage']['local_only'] is True
        assert done['usage']['budget_reason'] == 'daily_token_budget'
        assert transport.calls == 0
        assert rows(database, 'SELECT SUM(total_tokens) FROM usage_ledger') == [(9,)]
    finally:
        database.close()


def test_monthly_cost_reservation_blocks_a_request(tmp_path):
    transport = ScriptedTransport([stream_for(structured(text='不该出现'))])
    store, database, orchestrator, ctx = make_env(tmp_path, transport)
    try:
        store.update({'ai': {'monthly_cost_budget': 0.00001,
                             'input_price_per_million': 1.0,
                             'output_price_per_million': 1.0}})
        send(store, ctx, '讲个笑话吧')
        terminal, done = pump(orchestrator, ctx)
        assert terminal == 'chat.done'
        assert done['usage']['local_only'] is True
        assert done['usage']['budget_reason'] == 'monthly_cost_budget'
        assert transport.calls == 0
    finally:
        database.close()


def test_budget_hard_limit_switches_to_local_only(tmp_path):
    store, database, orchestrator, ctx = make_env(
        tmp_path, ScriptedTransport([stream_for(structured(text='不该出现'))]))
    try:
        store.update({'ai': {'daily_token_budget': 10}})
        with database.transaction() as connection:
            connection.execute(
                'INSERT INTO usage_ledger(prompt_tokens, completion_tokens, total_tokens) '
                'VALUES(15, 5, 20)')
        send(store, ctx, '讲个笑话吧')
        terminal, done = pump(orchestrator, ctx)
        assert terminal == 'chat.done'
        assert done['usage']['local_only'] is True
        assert done['usage']['budget_reason'] == 'daily_token_budget'
        assert orchestrator.transport.calls == 0
        assert '额度' in done['message']['content']
    finally:
        database.close()


def test_budget_ledger_records_cost(tmp_path):
    store = SettingsStore(tmp_path)
    database = Database(tmp_path / 'pet.db')
    try:
        ledger = BudgetLedger(database)
        recorded = ledger.record('r-1', 'deepseek-chat',
                                 {'prompt_tokens': 1000000,
                                  'completion_tokens': 1000000},
                                 {'input_price_per_million': 2.0,
                                  'output_price_per_million': 3.0,
                                  'currency': 'USD'})
        assert recorded['cost'] == pytest.approx(5.0)
        assert recorded['recorded'] is True
        assert rows(database, 'SELECT total_tokens, cost, currency FROM usage_ledger') \
            == [(2000000, pytest.approx(5.0), 'USD')]
        assert estimate_cost({'prompt_tokens': 0, 'completion_tokens': 0}, {}) == 0.0
        assert parse_usage({'prompt_tokens': -1, 'completion_tokens': True}) == {
            'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0}
    finally:
        database.close()


def test_hello_advertises_chat_as_available(tmp_path):
    store = SettingsStore(tmp_path)
    event, payload = dispatch({'type': 'hello', 'id': 'hello',
                               'payload': {'protocol_version': 1}}, store)
    assert event == 'ready'
    assert payload['features']['chat']['available'] is True


def test_runtime_tick_drains_streaming_frames(tmp_path):
    store = SettingsStore(tmp_path)
    database = Database(tmp_path / 'pet.db')
    store.ai_transport = ScriptedTransport([
        stream_for(structured(text='运行中的回复'))])
    store.ai_key_provider = lambda: FAKE_KEY
    frame = json.dumps({'version': 1, 'id': 'req-1', 'type': 'chat.send',
                        'payload': {'text': '讲个笑话吧'}},
                       ensure_ascii=False).encode('utf-8') + b'\n'
    output = io.BytesIO()
    runtime = Runtime(store, output, BoundedLog(io.BytesIO()), database,
                      tick_seconds=0.01)
    try:
        assert runtime.run(FrameThenIdleInput(frame)) == 0
    finally:
        database.close()
    replies = [json.loads(line) for line in
               output.getvalue().decode('utf-8').splitlines()]
    types = [reply['type'] for reply in replies]
    assert types[0] == 'accepted'
    assert types[-1] == 'chat.done'
    assert 'chat.delta' in types
    assert all(reply['id'] == 'req-1' for reply in replies)
    assert replies[-1]['payload']['message']['content'] == '运行中的回复'


def test_chat_cancel_without_request_id_is_a_noop(tmp_path):
    store = SettingsStore(tmp_path)
    assert dispatch({'type': 'chat.cancel', 'id': 'cancel', 'payload': {}},
                    store) == ('cancelled', {})


# Secrets and key commands


class MemorySecretStore:
    def __init__(self):
        self.value = None

    def save(self, key):
        self.value = key

    def load(self):
        if self.value is None:
            raise SecretsError('secrets_missing', 'No API key is configured.')
        return self.value

    def clear(self):
        had = self.value is not None
        self.value = None
        return had


def test_ai_key_commands_keep_the_key_out_of_settings(tmp_path):
    store = SettingsStore(tmp_path)
    store.secrets_store = MemorySecretStore()
    event, payload = dispatch({'type': 'ai.set_key', 'id': 'key',
                               'payload': {'key': FAKE_KEY}}, store)
    assert (event, payload) == ('result', {'saved': True})
    assert store.secrets_store.value == FAKE_KEY
    assert 'key' not in store.settings
    assert FAKE_KEY not in (tmp_path / 'settings.json').read_text('utf-8')
    event, payload = dispatch({'type': 'ai.clear_key', 'id': 'clear',
                               'payload': {}}, store)
    assert (event, payload) == ('result', {'cleared': True})
    assert store.secrets_store.value is None


def test_ai_test_uses_fake_transport(tmp_path):
    store = SettingsStore(tmp_path)
    store.secrets_store = MemorySecretStore()
    store.secrets_store.value = FAKE_KEY
    store.ai_transport = ScriptedTransport([json_response('pong')])
    event, payload = dispatch({'type': 'ai.test', 'id': 'test', 'payload': {}}, store)
    assert event == 'result'
    assert payload['ok'] is True
    assert payload['model'] == 'deepseek-chat'
    assert payload['provider'] == 'deepseek'


def test_ai_test_maps_provider_errors(tmp_path):
    store = SettingsStore(tmp_path)
    store.secrets_store = MemorySecretStore()
    store.secrets_store.value = FAKE_KEY
    store.ai_transport = ScriptedTransport([
        TransportError('http', 'HTTP 402', status=402,
                       body=b'{"error":{"message":"Insufficient Balance"}}')])
    with pytest.raises(ProtocolError) as caught:
        dispatch({'type': 'ai.test', 'id': 'test', 'payload': {}}, store)
    assert caught.value.code == 'payment_required'


@pytest.mark.skipif(sys.platform != 'win32', reason='DPAPI is Windows-only')
def test_dpapi_secret_roundtrip_has_no_plaintext(tmp_path):
    store = SecretStore(tmp_path)
    store.save(FAKE_KEY)
    raw = (tmp_path / 'secrets.bin').read_bytes()
    assert FAKE_KEY.encode('utf-8') not in raw
    assert store.load() == FAKE_KEY
    assert store.clear() is True
    assert not (tmp_path / 'secrets.bin').exists()


@pytest.mark.skipif(sys.platform != 'win32', reason='DPAPI is Windows-only')
def test_dpapi_rejects_corrupt_file(tmp_path):
    (tmp_path / 'secrets.bin').write_bytes(b'PET3D-SECRETS-1\nbroken')
    store = SecretStore(tmp_path)
    with pytest.raises(SecretsError) as caught:
        store.load()
    assert caught.value.code == 'secrets_unreadable'


@pytest.mark.skipif(sys.platform == 'win32', reason='non-Windows behavior')
def test_secret_store_refuses_plaintext_off_windows(tmp_path):
    store = SecretStore(tmp_path)
    with pytest.raises(SecretsError) as caught:
        store.save(FAKE_KEY)
    assert caught.value.code == 'secrets_unsupported'
    assert not (tmp_path / 'secrets.bin').exists()
