"""Chat orchestration: local routing, worker-thread streaming and events.

The main thread prepares the prompt, checks budgets and reads the key. A worker
thread performs only the network call. Events are queued from the worker and
drained by ``poll`` on the service thread, which owns SQLite writes and frame
emission. One repair retry is allowed for an invalid structured reply; provider
failures retry per ``ai.max_retries`` before the circuit sees the failure.
"""

import queue
import re
import sqlite3
import threading
import time

from service.ai.budget import BudgetLedger, reservation
from service.ai.circuit import CircuitBreaker
from service.ai.prompt import build_messages
from service.ai.provider import OpenAICompatibleProvider, ProviderError
from service.ai.router import Router
from service.ai.schema import SchemaError, parse_structured, repair_instruction
from service.expression import style
from service.memory import writer
from service.memory.store import MemoryStore
from service.secrets import SecretsError, load_key_for
from service.state import character

SAFE_FALLBACK = '我现在有点跟不上，等会儿再聊好吗？'
SAFE_ERRORS = {
    'missing_key': '还没有配置 AI 密钥，请先在设置里填写。',
    'secrets_missing': '还没有配置 AI 密钥，请先在设置里填写。',
    'secrets_unsupported': '这台设备还不支持安全保存密钥。',
    'secrets_unreadable': '保存的密钥读取失败，请重新填写。',
    'circuit_open': '我有点累了，稍后再试好吗？',
    'rate_limited': '我现在有点忙，稍后再试好吗？',
    'payment_required': 'AI 服务暂时不可用，请检查账户余额。',
    'auth_error': 'AI 密钥无效，请重新填写。',
    'timeout': '等得太久了，稍后再试好吗？',
    'connection_error': '网络好像断了，稍后再试好吗？',
    'server_error': 'AI 服务暂时不可用，稍后再试好吗？',
    'invalid_response': '我这次没组织好语言，稍后再说好吗？',
    'provider_rejected': '这个请求 AI 服务没有接受，换个说法试试？',
    'provider_error': 'AI 服务暂时不可用，稍后再试好吗？',
    'model_not_found': '配置的模型不可用，请检查设置。',
    'cancelled': '好的，先不说了。',
    'internal_error': '我这边出了点小问题，稍后再试好吗？',
}
LOCAL_ONLY_REPLY = '今天的对话额度用完了，我先陪你聊到这里。'

_TEXT_MARKER = re.compile(r'(?:^|[,{])\s*"text"\s*:\s*"')
_SIMPLE_ESCAPES = {'"': '"', '\\': '\\', '/': '/', 'b': '\b',
                   'f': '\f', 'n': '\n', 'r': '\r', 't': '\t'}


def _safe_message(code):
    return SAFE_ERRORS.get(str(code), SAFE_FALLBACK)


class _Event:
    __slots__ = ('kind', 'request_id', 'data')

    def __init__(self, kind, request_id, data):
        self.kind = kind
        self.request_id = request_id
        self.data = data


class _Active:
    __slots__ = ('request_id', 'conversation_id', 'ctx', 'settings', 'cancel',
                 'on_final')

    def __init__(self, request_id, conversation_id, ctx, settings, on_final):
        self.request_id = request_id
        self.conversation_id = conversation_id
        self.ctx = ctx
        self.settings = settings
        self.cancel = threading.Event()
        self.on_final = on_final


class _TextGate:
    """Best-effort incremental extraction of the JSON ``text`` value."""

    def __init__(self):
        self.raw = ''
        self.emitted = ''

    def start_attempt(self):
        self.raw = ''

    def feed(self, chunk):
        self.raw += chunk
        prefix = _extract_text_prefix(self.raw)
        if not prefix.startswith(self.emitted):
            return ''
        delta = prefix[len(self.emitted):]
        self.emitted = prefix
        return delta


def _extract_text_prefix(raw):
    marker = _TEXT_MARKER.search(raw)
    if marker is None:
        return ''
    value, _complete = _decode_partial_string(raw, marker.end())
    return value


def _decode_partial_string(raw, index):
    result = []
    i = index
    length = len(raw)
    while i < length:
        char = raw[i]
        if char == '"':
            return ''.join(result), True
        if char != '\\':
            result.append(char)
            i += 1
            continue
        if i + 1 >= length:
            break
        escape = raw[i + 1]
        if escape in _SIMPLE_ESCAPES:
            result.append(_SIMPLE_ESCAPES[escape])
            i += 2
            continue
        if escape != 'u':
            break
        if i + 6 > length:
            break
        code = raw[i + 2:i + 6]
        if not re.fullmatch(r'[0-9a-fA-F]{4}', code):
            break
        number = int(code, 16)
        i += 6
        if 0xD800 <= number <= 0xDBFF:
            if i + 6 <= length and raw[i:i + 2] == '\\u':
                low_code = raw[i + 2:i + 6]
                if re.fullmatch(r'[0-9a-fA-F]{4}', low_code):
                    low = int(low_code, 16)
                    if 0xDC00 <= low <= 0xDFFF:
                        number = 0x10000 + ((number - 0xD800) << 10) + (low - 0xDC00)
                        i += 6
            if 0xD800 <= number <= 0xDFFF:
                continue
        elif 0xDC00 <= number <= 0xDFFF:
            continue
        result.append(chr(number))
    return ''.join(result), False


class ChatOrchestrator:
    def __init__(self, store, database=None, transport=None, circuit=None,
                 budget=None, retry_delay=None, provider_factory=None,
                 router=None):
        self.store = store
        self.database = database
        self.transport = transport if transport is not None else getattr(store, 'ai_transport', None)
        self.circuit = circuit if circuit is not None else getattr(store, 'ai_circuit', None)
        if self.circuit is None:
            self.circuit = CircuitBreaker()
        self.budget = budget if budget is not None else BudgetLedger(database)
        if retry_delay is None:
            retry_delay = getattr(store, 'ai_retry_delay', 0.2)
        self.retry_delay = max(0.0, float(retry_delay))
        self.provider_factory = provider_factory or self._default_provider
        self.router = router or Router(local_model=bool(getattr(store, 'local_model', False)))
        self._active = {}
        self._events = queue.Queue()
        self._tick_registered = False

    def _default_provider(self, ai_settings):
        return OpenAICompatibleProvider(ai_settings, transport=self.transport)

    def attach(self, ctx):
        if self._tick_registered:
            return
        register = getattr(ctx, 'register_tick', None)
        if register is None:
            return
        try:
            register(self.poll)
        except RuntimeError:
            return
        self._tick_registered = True

    def submit(self, request_id, text, conversation_id, ctx, messages=None,
               on_final=None):
        try:
            settings = self.store.settings
        except Exception:
            settings = {}
        active = _Active(request_id, conversation_id, ctx, settings, on_final)
        self._active[request_id] = active

        route = self.router.route(text)
        if route is not None:
            self._events.put(_Event('local_done', request_id, route))
            return
        if self.router.local_model:
            self._events.put(_Event('error', request_id, {
                'code': 'not_available',
                'message': '本地模型尚未接入。',
                'retryable': False,
            }))
            return

        ai_settings = settings.get('ai') if isinstance(settings.get('ai'), dict) else {}
        if messages is None:
            messages = build_messages(self.database, conversation_id, text, settings)
        self._log_prompt(settings, messages)
        budget = self.budget.status(ai_settings, reservation(messages, ai_settings))
        if budget['local_only']:
            self._events.put(_Event('local_only', request_id, budget))
            return

        try:
            key = load_key_for(self.store)
        except SecretsError as exc:
            self._events.put(_Event('error', request_id, {
                'code': exc.code,
                'message': _safe_message(exc.code),
                'retryable': False,
            }))
            return

        if not self.circuit.allow():
            self._events.put(_Event('error', request_id, {
                'code': 'circuit_open',
                'message': _safe_message('circuit_open'),
                'retryable': True,
            }))
            return

        worker = threading.Thread(target=self._worker,
                                  args=(active, messages, settings, key, budget),
                                  name='chat-' + request_id, daemon=True)
        try:
            worker.start()
        except RuntimeError:
            self._events.put(_Event('error', request_id, {
                'code': 'internal_error',
                'message': _safe_message('internal_error'),
                'retryable': True,
            }))

    def _worker(self, active, messages, settings, key, budget_status):
        ai_settings = settings.get('ai') if isinstance(settings.get('ai'), dict) else {}
        try:
            provider = self.provider_factory(ai_settings)
        except Exception:
            self._events.put(_Event('error', active.request_id, {
                'code': 'internal_error',
                'message': _safe_message('internal_error'),
                'retryable': False,
            }))
            return
        max_retries = ai_settings.get('max_retries', 1)
        max_retries = max_retries if type(max_retries) is int else 1
        max_retries = max(0, min(max_retries, 10))
        attempt = 0
        repaired = False
        gate = _TextGate()
        current_messages = messages
        last_usage = {}
        model = ''
        while True:
            if active.cancel.is_set():
                self._events.put(_Event('cancelled', active.request_id, {}))
                return
            gate.start_attempt()

            def on_delta(piece):
                if active.cancel.is_set():
                    return
                delta = gate.feed(piece)
                if delta:
                    self._events.put(_Event('delta', active.request_id,
                                            {'text': delta}))

            try:
                completion = provider.stream_chat(
                    current_messages, key, on_delta=on_delta,
                    cancel_event=active.cancel)
            except ProviderError as exc:
                if exc.code == 'cancelled' or active.cancel.is_set():
                    self._events.put(_Event('cancelled', active.request_id, {}))
                    return
                if exc.retryable and attempt < max_retries:
                    attempt += 1
                    self._sleep(attempt)
                    continue
                self.circuit.record_failure(exc.code)
                self._events.put(_Event('error', active.request_id, {
                    'code': exc.code,
                    'message': _safe_message(exc.code),
                    'retryable': exc.retryable,
                    'detail': exc.message,
                }))
                return
            except Exception:
                self._events.put(_Event('error', active.request_id, {
                    'code': 'internal_error',
                    'message': _safe_message('internal_error'),
                    'retryable': False,
                }))
                return

            last_usage = completion.usage
            model = completion.model
            try:
                structured = parse_structured(completion.text)
            except SchemaError as exc:
                if not repaired:
                    repaired = True
                    current_messages = list(messages) + [
                        {'role': 'assistant', 'content': completion.text[:2000]},
                        {'role': 'user', 'content': repair_instruction(exc)},
                    ]
                    continue
                self.circuit.record_success()
                self._events.put(_Event('degraded', active.request_id, {
                    'text': gate.emitted or SAFE_FALLBACK,
                    'usage': last_usage,
                    'model': model,
                    'budget_warning': budget_status.get('warning', False),
                    'budget_reason': budget_status.get('reason', ''),
                }))
                return

            self.circuit.record_success()
            remainder = ''
            if structured['text'].startswith(gate.emitted):
                remainder = structured['text'][len(gate.emitted):]
            elif not gate.emitted:
                remainder = structured['text']
            if remainder:
                self._events.put(_Event('delta', active.request_id,
                                        {'text': remainder}))
            self._events.put(_Event('done', active.request_id, {
                'text': structured['text'],
                'speak': structured['speak'],
                'action': structured['action'],
                'confidence': structured['confidence'],
                'memory_candidates': structured['memory_candidates'],
                'state_suggestion': structured['state_suggestion'],
                'usage': last_usage,
                'model': model,
                'budget_warning': budget_status.get('warning', False),
                'budget_reason': budget_status.get('reason', ''),
            }))
            return

    def _sleep(self, attempt):
        if self.retry_delay <= 0:
            return
        delay = min(self.retry_delay * (2 ** max(0, attempt - 1)), 2.0)
        time.sleep(delay)

    def poll(self, now=None):
        while True:
            try:
                event = self._events.get_nowait()
            except queue.Empty:
                return
            try:
                self._handle_event(event)
            except Exception:
                active = self._active.pop(event.request_id, None)
                if active is not None:
                    self._emit(active.ctx, 'chat.error', {
                        'code': 'internal_error',
                        'message': _safe_message('internal_error'),
                        'retryable': False,
                    })

    def _handle_event(self, event):
        active = self._active.get(event.request_id)
        if active is None:
            return
        if event.kind == 'delta':
            if active.cancel.is_set():
                return
            self._emit(active.ctx, 'chat.delta',
                       {'text': event.data.get('text', '')})
            return
        if event.kind == 'local_done':
            route = event.data
            text = route.reply
            memory_ops = None
            if route.memory_text:
                text, memory_ops = self._remember(route.memory_text, active.settings)
            payload = self._done_payload(active, {
                'text': text,
                'speak': text if route.action in ('wave', 'walk') else '',
                'action': route.action,
                'confidence': 1.0,
                'memory_candidates': [],
                'state_suggestion': {},
            }, local=True)
            if memory_ops is not None:
                payload['memory_ops'] = memory_ops
            self._notify(active, 'done', {'text': text,
                                          'conversation_id': active.conversation_id})
            self._emit(active.ctx, 'chat.done', payload)
            self._active.pop(active.request_id, None)
            return
        if event.kind == 'local_only':
            data = event.data
            payload = self._done_payload(active, {
                'text': LOCAL_ONLY_REPLY,
                'speak': '',
                'action': '',
                'confidence': 1.0,
                'memory_candidates': [],
                'state_suggestion': {},
            }, local=True, budget=data)
            self._notify(active, 'done', {'text': LOCAL_ONLY_REPLY,
                                          'conversation_id': active.conversation_id})
            self._emit(active.ctx, 'chat.done', payload)
            self._active.pop(active.request_id, None)
            return
        if event.kind == 'done':
            payload = self._done_payload(active, event.data)
            text = payload['message']['content']
            self._notify(active, 'done', {'text': text,
                                          'conversation_id': active.conversation_id})
            self._emit(active.ctx, 'chat.done', payload)
            self._active.pop(active.request_id, None)
            return
        if event.kind == 'degraded':
            data = dict(event.data)
            data['degraded'] = True
            payload = self._done_payload(active, data)
            text = payload['message']['content']
            self._notify(active, 'done', {'text': text,
                                          'conversation_id': active.conversation_id})
            self._emit(active.ctx, 'chat.done', payload)
            self._active.pop(active.request_id, None)
            return
        if event.kind == 'cancelled':
            self._notify(active, 'cancelled', {})
            self._emit(active.ctx, 'chat.error', {
                'code': 'cancelled',
                'message': _safe_message('cancelled'),
                'retryable': False,
            })
            self._active.pop(active.request_id, None)
            return
        if event.kind == 'error':
            data = event.data
            error = {
                'code': str(data.get('code') or 'internal_error'),
                'message': str(data.get('message') or SAFE_FALLBACK),
                'retryable': bool(data.get('retryable', False)),
            }
            self._notify(active, 'error', error)
            self._emit(active.ctx, 'chat.error', error)
            self._active.pop(active.request_id, None)

    def _done_payload(self, active, data, local=False, budget=None):
        settings = active.settings if isinstance(active.settings, dict) else {}
        memory = settings.get('memory') if isinstance(settings.get('memory'), dict) else {}
        ai_settings = settings.get('ai') if isinstance(settings.get('ai'), dict) else {}
        text = data.get('text') if isinstance(data.get('text'), str) else ''
        text = self._polish(settings, text)
        if local:
            usage = {
                'prompt_tokens': 0,
                'completion_tokens': 0,
                'total_tokens': 0,
                'cost': 0.0,
                'currency': ai_settings.get('currency') or 'USD',
                'recorded': False,
            }
            if budget is not None:
                usage['local_only'] = True
                usage['budget_reason'] = budget.get('reason', '')
            else:
                usage['local'] = True
        else:
            usage = self.budget.record(active.request_id, data.get('model', ''),
                                       data.get('usage') or {}, ai_settings)
            usage['provider'] = ai_settings.get('provider') or 'deepseek'
            if data.get('budget_warning'):
                usage['budget_warning'] = True
            if data.get('budget_reason'):
                usage['budget_reason'] = data.get('budget_reason')
            if data.get('degraded'):
                usage['degraded'] = True
        suggestion = data.get('state_suggestion')
        state_delta = self._apply_state_suggestion(suggestion)
        message = {
            'role': 'assistant',
            'content': text,
            'text': text,
            'speak': data.get('speak') if isinstance(data.get('speak'), str) else '',
            'action': data.get('action') if isinstance(data.get('action'), str) else '',
            'confidence': data.get('confidence', 0.0),
        }
        return {
            'message': message,
            'usage': usage,
            'memory_ops': self._memory_ops(data.get('memory_candidates'), memory),
            'state_delta': state_delta,
            'conversation_id': active.conversation_id,
        }

    def _polish(self, settings, text):
        expression = settings.get('expression') if isinstance(settings, dict) else {}
        if not isinstance(expression, dict) or not expression.get('enabled', True):
            return text
        return style.polish(text)

    def _apply_state_suggestion(self, suggestion):
        if not isinstance(suggestion, dict) or not suggestion:
            return {}
        if self.database is None:
            return {'applied': False, 'suggestion': suggestion}
        try:
            with self.database.transaction() as connection:
                state = character.apply_delta(connection, suggestion)
        except (sqlite3.Error, ValueError, TypeError):
            return {'applied': False, 'suggestion': suggestion}
        return {
            'applied': True,
            'suggestion': suggestion,
            'state': {'pad': dict(state['pad']), 'psych': dict(state['psych'])},
        }

    def _memory_ops(self, candidates, memory_settings):
        if not isinstance(memory_settings, dict) or not memory_settings.get('enabled', True):
            return []
        if not isinstance(candidates, list):
            return []
        try:
            max_chars = max(1, int(memory_settings.get('max_entry_chars', 500)))
        except (TypeError, ValueError):
            max_chars = 500
        auto_capture = bool(memory_settings.get('auto_capture', False))
        ops = []
        for item in candidates[:8]:
            if not isinstance(item, str) or not item.strip():
                continue
            content = item.strip()[:max_chars]
            op = {'operation': 'propose', 'status': 'pending', 'content': content}
            if auto_capture:
                outcome = self._propose_memory(content, memory_settings, source_type='chat')
                if outcome is None:
                    op['status'] = 'failed'
                else:
                    op['status'] = str(outcome.get('status') or 'rejected')
                    row = outcome.get('memory') or {}
                    if row.get('id'):
                        op['memory_id'] = row['id']
                    if outcome.get('reason'):
                        op['reason'] = outcome['reason']
            ops.append(op)
        return ops

    def _remember(self, text, settings):
        settings = settings if isinstance(settings, dict) else {}
        memory = settings.get('memory') if isinstance(settings.get('memory'), dict) else {}
        if not memory.get('enabled', True):
            return '记忆功能现在是关闭的，我没法记住。', []
        outcome = self._propose_memory(text, memory, source_type='chat')
        if outcome is None:
            return ('我想记下来，但没能保存。',
                    [{'operation': 'propose', 'status': 'failed', 'content': text}])
        status = str(outcome.get('status') or 'rejected')
        op = {'operation': 'propose', 'status': status, 'content': text}
        row = outcome.get('memory') or {}
        if row.get('id'):
            op['memory_id'] = row['id']
        if outcome.get('reason'):
            op['reason'] = outcome['reason']
        if status in ('committed', 'merged'):
            return '我记住了。', [op]
        if status == 'queued':
            return '我先记下来了，稍后会保存到记忆里。', [op]
        return '这条我没能记住。', [op]

    def _propose_memory(self, text, memory_settings, source_type='chat'):
        if self.database is None:
            return None
        try:
            store = getattr(self.database, 'memory_store_v3', None)
            if store is None:
                store = MemoryStore(self.database)
                setattr(self.database, 'memory_store_v3', store)
            return writer.propose(store, text, source_type=source_type,
                                  settings=memory_settings)
        except (sqlite3.Error, OSError, ValueError, UnicodeError):
            return None

    def _log_prompt(self, settings, messages):
        privacy = settings.get('privacy') if isinstance(settings, dict) else {}
        if not isinstance(privacy, dict) or not privacy.get('log_prompts', False):
            return
        log = getattr(self.store, 'log', None)
        if log is None:
            return
        parts = []
        for item in messages if isinstance(messages, list) else []:
            if isinstance(item, dict) and isinstance(item.get('content'), str):
                parts.append(item['content'])
        try:
            log.write('chat prompt: ' + ' | '.join(parts))
        except Exception:
            pass

    def _notify(self, active, kind, data):
        callback = active.on_final
        if callback is None:
            return
        try:
            callback(kind, data)
        except Exception:
            pass

    @staticmethod
    def _emit(ctx, event, payload):
        try:
            ctx.emit(event, payload)
            return
        except RuntimeError:
            pass
        except Exception:
            return
        try:
            ctx.post(event, payload)
        except Exception:
            pass

    def cancel(self, request_id):
        active = self._active.get(request_id)
        if active is None:
            return False
        active.cancel.set()
        return True

    def pending_count(self):
        return len(self._active)


def get_orchestrator(store, database=None):
    orchestrator = getattr(store, '_chat_orchestrator', None)
    if orchestrator is None:
        orchestrator = ChatOrchestrator(store, database)
        store._chat_orchestrator = orchestrator
        return orchestrator
    if database is not None and orchestrator.database is None:
        orchestrator.database = database
        orchestrator.budget.database = database
    return orchestrator
