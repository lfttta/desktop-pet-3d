"""Chat orchestration: local routing, worker-thread streaming and events.

The main thread prepares the prompt, checks budgets and reads the key. A worker
thread performs only the network call. Events are queued from the worker and
drained by ``poll`` on the service thread, which owns SQLite writes and frame
emission. One repair retry is allowed for an invalid structured reply; provider
failures retry per ``ai.max_retries`` before the circuit sees the failure.

并发聊天受固定大小的工作线程池限制；预算检查-预留是原子的，在途请求的
最坏用量会被后续请求看见。``close`` 在退出前取消并限时等待工作线程，然后
把已知的在途用量尽力写入账本，避免付费调用漏记账。
"""

import queue
import sqlite3
import threading
import time
import traceback

from service.ai.budget import BudgetLedger, reservation
from service.ai.circuit import CircuitBreaker
from service.ai.prompt import build_messages
from service.ai.provider import OpenAICompatibleProvider, ProviderError
from service.ai.router import Router
from service.ai.schema import SchemaError, parse_structured, repair_instruction
from service.ai.stream_text import TextGate as _TextGate
from service.expression import style
from service.memory import writer
from service.memory.store import DEFAULT_CATEGORY, DEFAULT_IMPORTANCE, MemoryStore
from service.secrets import SecretsError, load_key_for
from service.state import character, growth as growth_state

SAFE_FALLBACK = '我现在有点跟不上，等会儿再聊好吗？'
SAFE_ERRORS = {
    'missing_key': '还没有配置 AI 密钥，请先在设置里填写。',
    'secrets_missing': '还没有配置 AI 密钥，请先在设置里填写。',
    'secrets_unsupported': '这台设备还不支持安全保存密钥。',
    'secrets_unreadable': '保存的密钥读取失败，请重新填写。',
    'circuit_open': '我有点累了，稍后再试好吗？',
    'rate_limited': '我现在有点忙，稍后再试好吗？',
    'busy': '我现在有点忙，稍后再试好吗？',
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
# 同时进行的聊天网络请求上限；超出时立即回复 busy，而不是无限开线程。
MAX_CHAT_WORKERS = 4
SHUTDOWN_JOIN_SECONDS = 0.2


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
                 'on_final', 'worker', 'usage', 'model', 'recorded')

    def __init__(self, request_id, conversation_id, ctx, settings, on_final):
        self.request_id = request_id
        self.conversation_id = conversation_id
        self.ctx = ctx
        self.settings = settings
        self.cancel = threading.Event()
        self.on_final = on_final
        self.worker = None
        self.usage = None
        self.model = ''
        self.recorded = False


class ChatOrchestrator:
    def __init__(self, store, database=None, transport=None, circuit=None,
                 budget=None, retry_delay=None, provider_factory=None,
                 router=None, memory_store=None):
        self.store = store
        self.database = database
        self.memory_store = memory_store
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
        self._slots = threading.BoundedSemaphore(MAX_CHAT_WORKERS)
        self._closed = False

    def _default_provider(self, ai_settings):
        return OpenAICompatibleProvider(ai_settings, transport=self.transport)

    def attach(self, ctx):
        if not self._tick_registered:
            register = getattr(ctx, 'register_tick', None)
            if register is not None:
                try:
                    register(self.poll)
                except RuntimeError:
                    pass
                else:
                    self._tick_registered = True
        # Growth evidence comes from chat memory candidates, so the periodic
        # scan is activated by chat as well as by the growth commands.
        try:
            from service.features import growth as growth_feature
            growth_feature.register_tick(ctx, store=self.store)
        except Exception:
            pass

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
            messages = build_messages(self.database, conversation_id, text,
                                      settings, self.store)
        self._log_prompt(settings, messages)
        reserve = reservation(messages, ai_settings)
        budget = self.budget.status(ai_settings, reserve)
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

        if self._closed or not self._slots.acquire(blocking=False):
            self._events.put(_Event('error', request_id, {
                'code': 'busy',
                'message': _safe_message('busy'),
                'retryable': True,
            }))
            return
        # 检查通过后立即原子预留在途用量，后续并发请求能看到这笔预留。
        self.budget.reserve(request_id, reserve)

        worker = threading.Thread(target=self._worker,
                                  args=(active, messages, settings, key, budget),
                                  name='chat-' + request_id, daemon=True)
        active.worker = worker
        try:
            worker.start()
        except RuntimeError:
            self._slots.release()
            self.budget.release(request_id)
            self._events.put(_Event('error', request_id, {
                'code': 'internal_error',
                'message': _safe_message('internal_error'),
                'retryable': True,
            }))

    def _worker(self, active, messages, settings, key, budget_status):
        try:
            self._worker_inner(active, messages, settings, key, budget_status)
        finally:
            self._slots.release()

    def _worker_inner(self, active, messages, settings, key, budget_status):
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

        def note_usage(usage, usage_model=''):
            # 流式过程中一旦拿到用量就记到 active 上，退出时尽力补账。
            active.usage = usage
            if usage_model:
                active.model = usage_model

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
                    cancel_event=active.cancel, on_usage=note_usage)
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
            active.usage = last_usage
            active.model = model
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
            except Exception as exc:
                self._log_error('chat poll failed: %s\n%s'
                                % (exc, traceback.format_exc()))
                active = self._active.pop(event.request_id, None)
                if active is not None:
                    self.budget.release(active.request_id)
                    self._emit(active.ctx, 'chat.error', {
                        'code': 'internal_error',
                        'message': _safe_message('internal_error'),
                        'retryable': False,
                    })

    def _log_error(self, message):
        log = getattr(self.store, 'log', None)
        if log is None:
            return
        try:
            log.write(message)
        except Exception:
            pass

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
        # 终态事件：释放该请求的在途预算预留（幂等）。
        self.budget.release(active.request_id)
        if event.kind == 'local_done':
            route = event.data
            text = route.reply
            memory_ops = None
            timer_ops = None
            if route.memory_text:
                text, memory_ops = self._remember(route.memory_text, active.settings)
            if route.action in ('timer.set', 'timer.cancel'):
                text, timer_ops = self._apply_timer(active, route)
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
            if timer_ops is not None:
                payload['timer_ops'] = timer_ops
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
                                          'conversation_id': active.conversation_id,
                                          'local_only': True})
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
                                          'conversation_id': active.conversation_id,
                                          'degraded': True})
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
            if active.recorded:
                # 退出前的尽力补账已经写过这笔用量，避免重复记账。
                usage = {
                    'prompt_tokens': (active.usage or {}).get('prompt_tokens', 0),
                    'completion_tokens': (active.usage or {}).get('completion_tokens', 0),
                    'total_tokens': (active.usage or {}).get('total_tokens', 0),
                    'cost': 0.0,
                    'currency': ai_settings.get('currency') or 'USD',
                    'recorded': True,
                }
            else:
                usage = self.budget.record(active.request_id, data.get('model', ''),
                                           data.get('usage') or {}, ai_settings)
                active.recorded = True
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
            'presentation': character.presentation(state),
        }

    @staticmethod
    def _candidate_entry(item, max_chars):
        """Normalize one string or structured memory candidate.

        Structured entries carry the category/importance/source_type the writer
        needs; plain strings keep the old text-only shape so existing clients
        and stored payloads stay compatible.
        """
        if isinstance(item, str):
            content = item.strip()
            if not content:
                return None
            return {'content': content[:max_chars]}
        if not isinstance(item, dict):
            return None
        raw = item.get('content', item.get('text'))
        if not isinstance(raw, str) or not raw.strip():
            return None
        category = growth_state.candidate_category(item.get('category'))
        importance = item.get('importance', DEFAULT_IMPORTANCE)
        if type(importance) not in (int, float) or isinstance(importance, bool) \
                or not 0.0 <= float(importance) <= 1.0:
            importance = DEFAULT_IMPORTANCE
        source_type = item.get('source_type', 'chat')
        if not isinstance(source_type, str) or source_type not in writer.SOURCE_TYPES:
            source_type = 'chat'
        return {
            'content': raw.strip()[:max_chars],
            'category': category,
            'importance': float(importance),
            'source_type': source_type,
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
            entry = self._candidate_entry(item, max_chars)
            if entry is None:
                continue
            content = entry['content']
            op = {'operation': 'propose', 'status': 'pending', 'content': content}
            # Structured candidates expose the metadata so the app can pass it
            # back through memory.propose when the user presses 记住.
            for key in ('category', 'importance', 'source_type'):
                if key in entry:
                    op[key] = entry[key]
            if auto_capture:
                outcome = self._propose_memory(
                    content, memory_settings,
                    source_type=entry.get('source_type', 'chat'),
                    category=entry.get('category'),
                    importance=entry.get('importance'))
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
        outcome = self._propose_memory(text, memory, source_type='chat',
                                       category=DEFAULT_CATEGORY)
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

    def _apply_timer(self, active, route):
        """Write a locally routed reminder to the timers table.

        The router only recognises the phrase; the row has to be created here on
        the service thread, otherwise the heartbeat's due check never sees it and
        the reply promises a reminder that does not exist. A missing database or
        a storage error keeps an honest failure reply instead of "好的".
        """
        from service.features import timers

        database = getattr(active.ctx, 'database', None)
        if database is None:
            return ('提醒没能保存，存储暂时不可用。',
                    [{'operation': route.action, 'status': 'failed'}])
        if route.action == 'timer.set':
            minutes = route.timer_minutes
            if type(minutes) is not int or not 1 <= minutes <= timers.MAX_MINUTES:
                return ('这个提醒时间我没法设置。',
                        [{'operation': 'timer.set', 'status': 'failed',
                          'text': route.timer_text or ''}])
            detail = route.timer_text or timers.DEFAULT_TEXT
            try:
                timer = timers.create(database, minutes, detail)
            except (sqlite3.Error, OSError, ValueError, TypeError):
                return ('提醒没能保存，稍后再试好吗？',
                        [{'operation': 'timer.set', 'status': 'failed',
                          'text': detail}])
            return route.reply, [{'operation': 'timer.set', 'status': 'committed',
                                  'timer_id': timer['id'], 'due_at': timer['due_at'],
                                  'text': timer['text']}]
        try:
            cancelled = timers.cancel(database)
        except (sqlite3.Error, OSError, ValueError, TypeError):
            return ('取消失败，稍后再试好吗？',
                    [{'operation': 'timer.cancel', 'status': 'failed'}])
        return route.reply, [{'operation': 'timer.cancel', 'status': 'committed',
                              'cancelled': cancelled}]

    def _propose_memory(self, text, memory_settings, source_type='chat',
                        category=None, importance=None):
        if self.database is None:
            return None
        try:
            # An explicitly injected store wins; otherwise reuse the shared
            # per-database cache so each propose does not rerun the schema
            # check and dedupe-key backfill.
            store = self.memory_store
            if store is None:
                store = getattr(self.database, 'memory_store_v3', None)
            if store is None:
                store = MemoryStore(self.database)
                setattr(self.database, 'memory_store_v3', store)
            extra = {}
            if category is not None:
                extra['category'] = category
            if importance is not None:
                extra['importance'] = importance
            return writer.propose(store, text, source_type=source_type,
                                  settings=memory_settings, **extra)
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

    def close(self):
        """退出前取消在途请求，限时等待，并把已知用量尽力写入账本。"""
        self._closed = True
        active_list = list(self._active.values())
        for active in active_list:
            active.cancel.set()
        for active in active_list:
            worker = active.worker
            if worker is not None and worker.is_alive():
                try:
                    worker.join(timeout=SHUTDOWN_JOIN_SECONDS)
                except RuntimeError:
                    pass
        for active in active_list:
            self.budget.release(active.request_id)
            if active.recorded or not active.usage:
                continue
            settings = active.settings if isinstance(active.settings, dict) else {}
            ai_settings = settings.get('ai') if isinstance(settings.get('ai'), dict) else {}
            try:
                self.budget.record(active.request_id, active.model or '',
                                   active.usage, ai_settings)
                active.recorded = True
            except Exception:
                # 补账是尽力而为：失败也不能阻塞退出。
                continue
        self._active.clear()

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
