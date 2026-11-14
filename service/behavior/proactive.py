"""Desktop-aware proactive speaking policy and the cloud scheduler.

The policy maps one desktop state plus the user's desktop preferences to a
decision the scheduler can enforce: whether speaking is allowed, which phrase
categories are permitted and how much the budget must be tightened.

The scheduler itself is split in two halves. ``tick`` runs on the service
thread only: it drains finished results, decides whether a new message may be
attempted and performs every SQLite write and every posted frame. The worker
thread only loads the key, builds the prompt and calls the provider; it never
touches the database or the runtime. A single result queue hands finished work
back to ``tick``, so at most one provider call is ever in flight.

Logging here records event codes only. Message text, keys and window titles are
never passed to the logger.
"""

from collections import deque
from datetime import datetime, timezone
import logging
import queue
import sqlite3
import threading
import time

from service.ai import proactive as cloud_prompts
from service.ai.budget import BudgetLedger, reservation
from service.ai.provider import OpenAICompatibleProvider
from service.behavior import budget
from service.database import SUBJECT_USER_ID
from service.expression import phrases
from service.memory.policy import similarity
from service.secrets import load_key_for

SILENT_STATES = frozenset({'do_not_disturb', 'fullscreen', 'sleep_hours'})
DESKTOP_UNKNOWN_REASON = 'desktop_unknown'
# Categories the desktop policies may put on the wire. They are checked against
# the phrase library's whitelist so a typo here can never ask the selector for
# an intent no phrase carries; unknown names are dropped, never widened.
POLICY_CATEGORIES = ('care', 'idle', 'night')
POLICY_CATEGORY_NAMES = frozenset(POLICY_CATEGORIES)
SENT_HISTORY = 5
SIMILARITY_THRESHOLD = 0.8
CLOUD_EVENT = 'autonomy.bubble'
CLOUD_REQUEST_ID = 'autonomy'
JOIN_TIMEOUT_SECONDS = 1.0
# Quiet hours live in the autonomy group; only the idle threshold is a desktop
# policy value. Reading quiet hours from the desktop group (where the settings
# validator rejects them) silently kept the classify defaults.
AUTONOMY_CLASSIFY_KEYS = ('quiet_start', 'quiet_end')
DESKTOP_CLASSIFY_KEYS = ('idle_after_seconds', 'interaction_seconds')
# A failed provider call backs off exponentially so a broken endpoint is not
# billed once per scheduler interval.
FAILURE_BACKOFF_BASE_SECONDS = 60.0
FAILURE_BACKOFF_MAX_SECONDS = 3600.0
FAILURE_BACKOFF_MAX_DOUBLINGS = 10
# A fresh process stays quiet on the cloud path for this long; a cold start
# would otherwise let the first tick spend money before the user has settled in.
STARTUP_WARMUP_SECONDS = 90.0
# Proactive recall uses the same shape the chat prompt uses for memories.
MEMORY_LIMIT = 3
MEMORY_ENTRY_CHARS = 400
MEMORY_QUERY_CHARS = 200

_LOGGER = logging.getLogger(__name__)


def _text(value):
    return value if type(value) is str else ''


def _memory_texts(result):
    """Normalize a recall result to plain, bounded strings.

    ``search_for_context`` may return summaries (dicts) or already-rendered
    strings, so both shapes are accepted. Anything malformed is skipped rather
    than failing the message.
    """
    if type(result) is str:
        result = [result]
    if not isinstance(result, (list, tuple)):
        return []
    texts = []
    seen = set()
    for item in result:
        if type(item) is str:
            text = item
        elif isinstance(item, dict):
            text = item.get('text') or item.get('snippet') or item.get('content') or ''
        else:
            continue
        if type(text) is not str:
            continue
        text = text.strip()[:MEMORY_ENTRY_CHARS]
        if text and text not in seen:
            seen.add(text)
            texts.append(text)
    return texts


def _sensing_on(desktop_settings):
    """Desktop sensing counts as on only when the group explicitly enables it.

    A missing, empty or partially configured group therefore leaves the
    character on its pre-desktop behaviour instead of silencing it because a
    policy value happens to be absent.
    """
    if not isinstance(desktop_settings, dict):
        return False
    return desktop_settings.get('enabled') is True


def _policy_value(desktop_settings, key):
    """Return a configured policy value; an unknown or missing value is silent."""
    value = desktop_settings.get(key)
    return value if type(value) is str else ''


def _allowed(state, categories=(), min_factor=1.0, max_per_hour=None):
    # Only intents the phrase library whitelists may travel with a policy.
    # Unknown names are dropped rather than widened into "any category", so a
    # typo here silences that policy instead of speaking the wrong kind of line.
    allowed = []
    for category in categories:
        if category in POLICY_CATEGORY_NAMES and category not in allowed:
            allowed.append(category)
    return {
        'allowed': True,
        'reason': 'allowed',
        'categories': allowed,
        'min_factor': float(min_factor),
        'max_per_hour': max_per_hour,
        'state': state,
    }


def _unknown(state):
    return {
        'allowed': True,
        'reason': DESKTOP_UNKNOWN_REASON,
        'categories': [],
        'min_factor': 1.0,
        'max_per_hour': None,
        'state': state,
    }


def _silent(state):
    return {
        'allowed': False,
        'reason': state,
        'categories': [],
        'min_factor': 1.0,
        'max_per_hour': None,
        'state': state,
    }


def policy_for(state, autonomy_settings, desktop_settings=None):
    """Map a desktop state to one speaking policy.

    ``desktop_settings`` is the desktop settings group. Sensing is on only when
    that mapping carries ``enabled=True``; otherwise every state is treated as
    unknown so the character keeps its pre-desktop behaviour. A policy field
    with a missing or unrecognized value is treated as ``silent``.

    ``autonomy_settings`` is accepted for call-site symmetry: the per-state
    policy lives entirely in the desktop group.
    """
    del autonomy_settings
    state = _text(state) or 'unknown'
    if not _sensing_on(desktop_settings) or state == 'unknown':
        return _unknown(state)

    # Fullscreen and do-not-disturb are silent unless their own policy key
    # relaxes them to care-only; sleep hours are always silent. A missing or
    # unrecognized policy value therefore behaves exactly like 'silent'.
    if state == 'fullscreen':
        if _policy_value(desktop_settings, 'fullscreen_policy') == 'minimal':
            return _allowed(state, ['care'])
        return _silent(state)

    if state == 'do_not_disturb':
        if _policy_value(desktop_settings, 'dnd_policy') == 'minimal':
            return _allowed(state, ['care'])
        return _silent(state)

    if state in SILENT_STATES:
        return _silent(state)

    if state == 'active_work':
        policy = _policy_value(desktop_settings, 'work_focus_policy')
        if policy == 'normal':
            return _allowed(state)
        if policy == 'minimal':
            return _allowed(state, ['care'], min_factor=2.0, max_per_hour=1)
        return _silent(state)

    if state == 'idle':
        policy = _policy_value(desktop_settings, 'idle_policy')
        if policy == 'normal':
            return _allowed(state)
        if policy == 'gentle':
            return _allowed(state, ['care', 'idle', 'night'])
        return _silent(state)

    if state == 'active_interaction':
        return _allowed(state)

    # An unrecognized state is as good as unknown; never invent a restriction.
    return _unknown(state)


def _snapshot(monitor):
    if monitor is None:
        return {'known': False}
    getter = getattr(monitor, 'snapshot', None)
    if getter is None and callable(monitor):
        getter = monitor
    if getter is None:
        return {'known': False}
    try:
        value = getter()
    except Exception:
        return {'known': False}
    return value if isinstance(value, dict) else {'known': False}


def _classify_function():
    try:
        from service.desktop.monitor import classify
    except Exception:
        return None
    return classify if callable(classify) else None


def release_store_monitor(store):
    """Close and forget the monitor cached on ``store``.

    Used when desktop sensing is switched off: a cached monitor would keep the
    Win32 backend alive and could still be sampled through the status command.
    """
    monitor = getattr(store, 'desktop_monitor', None)
    if monitor is None:
        return
    close = getattr(monitor, 'close', None)
    if callable(close):
        try:
            close()
        except Exception:
            pass
    try:
        del store.desktop_monitor
    except AttributeError:
        pass


class ProactiveScheduler:
    """Service-thread scheduler with one background provider call at a time."""

    def __init__(self, runtime, store, database, monitor=None,
                 provider_factory=None, clock=None, interval_seconds=30.0,
                 rng=None, startup_grace_seconds=STARTUP_WARMUP_SECONDS,
                 started_at=None):
        self.runtime = runtime
        self.store = store
        self.database = database
        self.monitor = monitor
        self.provider_factory = provider_factory
        self.clock = clock if callable(clock) else time.monotonic
        self.interval_seconds = max(0.0, float(interval_seconds))
        self.rng = rng
        try:
            self.startup_grace_seconds = max(0.0, float(startup_grace_seconds))
        except (TypeError, ValueError):
            self.startup_grace_seconds = STARTUP_WARMUP_SECONDS
        self._started_at = self._start_reference(started_at)
        self.ledger = BudgetLedger(database)
        self._results = queue.Queue()
        self._sent = deque(maxlen=SENT_HISTORY)
        self._worker = None
        self._inflight = False
        self._cancel = threading.Event()
        self._closed = False
        self._last_attempt = None
        self._cloud_failures = 0
        self._cloud_retry_at = None

    # -- public surface -------------------------------------------------

    def tick(self, now=None):
        """Drain finished work and, when due, attempt one new message."""
        if self._closed:
            return
        moment = self._moment(now)
        self._drain(moment)
        if self._closed:
            return
        instant = self._instant(now)
        if self._last_attempt is not None:
            if instant - self._last_attempt < self.interval_seconds:
                return
        self._last_attempt = instant

        settings = self._settings()
        if not self._speaking_enabled(settings):
            return
        policy = self._current_policy(settings, moment)
        if policy is None or not policy['allowed']:
            return
        if not self._budget_allows(settings, policy, moment):
            return

        autonomy = self._group(settings, 'autonomy')
        if autonomy.get('cloud_enabled') is True:
            if self._startup_warmup_active(now):
                # A cold start must not spend money before the user has settled
                # in, so the cloud path stays quiet for the grace period.
                self._log_event('skipped_warmup')
                return
            if self._cloud_backoff_active(instant):
                # A failed endpoint is not retried (and re-billed) every tick.
                self._log_event('skipped_backoff')
                return
            self._start_cloud(settings, policy, moment)
            return
        self._post_local(moment, policy)

    def close(self):
        """Cancel pending work; no new task starts after this call."""
        if self._closed:
            return
        self._closed = True
        self._cancel.set()
        worker = self._worker
        if worker is not None and worker.is_alive():
            try:
                worker.join(timeout=JOIN_TIMEOUT_SECONDS)
            except RuntimeError:
                pass

    # -- settings and policy -------------------------------------------

    @staticmethod
    def _group(settings, name):
        value = settings.get(name)
        return value if isinstance(value, dict) else {}

    def _settings(self):
        settings = getattr(self.store, 'settings', None)
        return settings if isinstance(settings, dict) else {}

    @staticmethod
    def _speaking_enabled(settings):
        autonomy = ProactiveScheduler._group(settings, 'autonomy')
        if not autonomy.get('enabled'):
            return False
        # ``phrases_enabled`` only gates the local phrase fallback; the cloud
        # message is generated independently and must keep working without a
        # phrase library. ``expression.enabled`` stays the master switch.
        expression = ProactiveScheduler._group(settings, 'expression')
        if not expression.get('enabled'):
            return False
        return True

    @staticmethod
    def _phrases_enabled(settings):
        return ProactiveScheduler._group(settings, 'expression').get(
            'phrases_enabled') is True

    def _desktop_settings(self, settings):
        return self._group(settings, 'desktop')

    def _release_monitor(self):
        """Drop the cached monitor once the user turns desktop sensing off.

        Only the store-owned monitor is released; an explicitly injected
        monitor belongs to the caller.
        """
        if self.monitor is not None:
            return
        release_store_monitor(self.store)

    def _desktop_state(self, settings, moment):
        desktop = self._desktop_settings(settings)
        # Desktop sensing is off unless the group explicitly enables it; no
        # sample is read and no cached monitor survives the switch.
        if not _sensing_on(desktop):
            self._release_monitor()
            return 'unknown'
        classify = _classify_function()
        if classify is None:
            return 'unknown'
        autonomy = self._group(settings, 'autonomy')
        monitor = self.monitor if self.monitor is not None \
            else getattr(self.store, 'desktop_monitor', None)
        snapshot = _snapshot(monitor)
        options = {}
        # Quiet hours are autonomy settings, not desktop policy. Passing them
        # from the desktop group silently fell back to classify()'s 23:00-08:00.
        for key in AUTONOMY_CLASSIFY_KEYS:
            if key in autonomy:
                options[key] = autonomy[key]
        for key in DESKTOP_CLASSIFY_KEYS:
            if key in desktop:
                options[key] = desktop[key]
        options['now'] = moment
        last_user_at = budget.last_activity(self.database)
        if last_user_at:
            options['last_user_at'] = last_user_at
        try:
            result = classify(snapshot, **options)
        except Exception:
            return 'unknown'
        if isinstance(result, dict) and type(result.get('state')) is str:
            return result['state']
        return 'unknown'

    def _current_policy(self, settings, moment):
        state = self._desktop_state(settings, moment)
        return policy_for(state, self._group(settings, 'autonomy'),
                          self._desktop_settings(settings))

    def _budget_allows(self, settings, policy, moment):
        constraints = {
            'min_factor': policy['min_factor'],
            'max_per_hour': policy['max_per_hour'],
        }
        decision = budget.can_speak(self.database, self._group(settings, 'autonomy'),
                                    now=moment, constraints=constraints)
        return bool(decision.get('allowed'))

    def _cloud_budget_reason(self, settings, messages):
        """Return the ledger reason that forbids a paid call, or ''.

        The chat path gates every request on the daily token and monthly cost
        ledger; proactive generation must obey the same user limits.
        """
        ai_settings = self._group(settings, 'ai')
        try:
            status = self.ledger.status(ai_settings,
                                        reservation(messages, ai_settings))
        except Exception:
            return ''
        if status.get('local_only'):
            return str(status.get('reason') or 'budget')
        return ''

    # -- timing ---------------------------------------------------------

    def _moment(self, now):
        # Runtime tick hooks pass a monotonic float; budget uses wall-clock time.
        if isinstance(now, (int, float)) and not isinstance(now, bool):
            now = None
        return budget.normalize_now(now)

    def _instant(self, now):
        if isinstance(now, datetime):
            return now.timestamp()
        if isinstance(now, (int, float)) and not isinstance(now, bool):
            return float(now)
        try:
            return float(self.clock())
        except Exception:
            return time.monotonic()

    def _start_reference(self, started_at):
        """Return the instant the process-start grace period begins at."""
        if started_at is not None:
            try:
                return float(started_at)
            except (TypeError, ValueError):
                pass
        try:
            return float(self.clock())
        except Exception:
            return time.monotonic()

    def _startup_warmup_active(self, now):
        """True while a fresh process must not attempt a cloud message.

        The service runtime ticks with a monotonic float, matching the default
        clock. Explicit ``datetime`` ticks are wall-clock (used by tests and
        replays) and have no monotonic origin to compare against, so the grace
        period does not apply to them.
        """
        if self.startup_grace_seconds <= 0.0:
            return False
        if isinstance(now, datetime):
            return False
        try:
            elapsed = self._instant(now) - self._started_at
        except (TypeError, ValueError):
            return False
        return 0.0 <= elapsed < self.startup_grace_seconds

    # -- cloud generation ----------------------------------------------

    def _memory_query(self, history):
        """Return the latest user line, or the conversation theme.

        The transcript tail is only a couple of lines, so the newest user turn
        is read directly from storage; a conversation with no user turn falls
        back to its title (the configured "topic" of the conversation).
        """
        connection = getattr(self.database, 'connection', None)
        if connection is not None:
            try:
                row = connection.execute(
                    "SELECT content FROM messages WHERE subject_user_id=? "
                    "AND role='user' AND content<>'' ORDER BY rowid DESC LIMIT 1",
                    (SUBJECT_USER_ID,)).fetchone()
            except (AttributeError, TypeError, sqlite3.Error):
                row = None
            if row is not None and type(row[0]) is str and row[0].strip():
                return row[0].strip()[:MEMORY_QUERY_CHARS]
        for item in reversed(history if isinstance(history, list) else []):
            if not isinstance(item, dict) or item.get('role') != 'user':
                continue
            content = item.get('content')
            if type(content) is str and content.strip():
                return content.strip()[:MEMORY_QUERY_CHARS]
        if connection is not None:
            try:
                row = connection.execute(
                    "SELECT title FROM conversations WHERE subject_user_id=? "
                    "AND title<>'' ORDER BY rowid DESC LIMIT 1",
                    (SUBJECT_USER_ID,)).fetchone()
            except (AttributeError, TypeError, sqlite3.Error):
                row = None
            if row is not None and type(row[0]) is str and row[0].strip():
                return row[0].strip()[:MEMORY_QUERY_CHARS]
        return ''

    def _recall_memories(self, history, settings):
        """Return up to three memory snippets for the next proactive prompt.

        Every failure yields an empty list: recall is context, never a reason
        to withhold the message. ``search_for_context`` is preferred; older
        builds without it use the keyword search over a ``MemoryStore``.
        """
        if self.database is None:
            return []
        if self._group(settings, 'memory').get('enabled') is False:
            return []
        query = self._memory_query(history)
        if not query:
            return []
        try:
            from service.memory import search as memory_search
        except Exception:
            return []
        recall = getattr(memory_search, 'search_for_context', None)
        if callable(recall):
            # The per-entry cap is separate from the total budget so three
            # entries of up to MEMORY_ENTRY_CHARS each can reach the prompt.
            source = self.store if isinstance(
                getattr(self.store, 'settings', None), dict) else settings
            try:
                result = recall(self.database, query, source, MEMORY_LIMIT,
                                total_chars=MEMORY_LIMIT * MEMORY_ENTRY_CHARS,
                                entry_chars=MEMORY_ENTRY_CHARS)
            except Exception:
                self._log_event('memory_failed')
                return []
            return _memory_texts(result)
        try:
            return _memory_texts(self._keyword_memories(
                memory_search, query, settings))
        except Exception:
            self._log_event('memory_failed')
            return []

    def _keyword_memories(self, memory_search, query, settings):
        from service.memory.store import MemoryStore
        store = getattr(self.database, 'memory_store_v3', None)
        if store is None:
            store = MemoryStore(self.database)
            try:
                setattr(self.database, 'memory_store_v3', store)
            except AttributeError:
                pass
        return memory_search.search(store, query, MEMORY_LIMIT, settings=settings)

    def _start_cloud(self, settings, policy, moment):
        if self._inflight:
            return
        # SQLite has one connection and it belongs to the service thread, so
        # the short transcript and the memory recall are read here; the worker
        # only performs the network call. Building the prompt here also lets
        # the token/cost ledger reserve the worst-case usage before any money
        # is spent.
        try:
            history = cloud_prompts.recent_history(
                self.database, cloud_prompts.DEFAULT_HISTORY_LIMIT)
        except Exception:
            history = []
        memories = self._recall_memories(history, settings)
        state_label = '' if policy['state'] == 'unknown' else policy['state']
        try:
            # An unknown state (sensing off or unreadable) carries no label at
            # all; a real desktop state is the only thing worth telling the
            # model, and it must never leak to the user.
            messages = cloud_prompts.build_messages(
                settings, settings.get('persona_id'), state_label, history,
                memories)
        except Exception:
            messages = None
        if messages is not None:
            reason = self._cloud_budget_reason(settings, messages)
            if reason:
                self._log_event('dropped_ledger', 'reason=' + reason)
                self._post_local(moment, policy)
                return
        worker = threading.Thread(target=self._run_cloud,
                                  args=(settings, messages, history,
                                        state_label, memories),
                                  name='proactive-cloud', daemon=True)
        self._inflight = True
        self._worker = worker
        try:
            worker.start()
        except RuntimeError:
            self._inflight = False
            self._worker = None
            self._log_event('start_failed')
            self._post_local(moment, policy)

    def _run_cloud(self, settings, messages, history, state_label='',
                   memories=None):
        result = {'kind': 'local'}
        try:
            key = load_key_for(self.store)
            if messages is None:
                messages = cloud_prompts.build_messages(
                    settings, settings.get('persona_id'), state_label, history,
                    memories)
            generated = cloud_prompts.generate(
                self._provider(settings), key, messages, cancel_event=self._cancel)
            if generated is not None:
                result = {
                    'kind': 'cloud',
                    'text': generated['text'],
                    'usage': generated['usage'],
                    'model': generated['model'],
                }
        except Exception:
            result = {'kind': 'local'}
        if self._cancel.is_set():
            return
        self._results.put(result)

    def _provider(self, settings):
        ai_settings = self._group(settings, 'ai')
        if self.provider_factory is not None:
            return self.provider_factory(ai_settings)
        transport = getattr(self.store, 'ai_transport', None)
        return OpenAICompatibleProvider(ai_settings, transport=transport)

    def _cloud_backoff_active(self, instant):
        retry_at = self._cloud_retry_at
        return retry_at is not None and instant < retry_at

    def _note_cloud_failure(self, instant):
        """Back off exponentially so a broken endpoint is not billed per tick."""
        self._cloud_failures += 1
        doublings = min(self._cloud_failures - 1, FAILURE_BACKOFF_MAX_DOUBLINGS)
        delay = min(FAILURE_BACKOFF_MAX_SECONDS,
                    FAILURE_BACKOFF_BASE_SECONDS * (2 ** doublings))
        self._cloud_retry_at = instant + delay

    def _note_cloud_success(self):
        self._cloud_failures = 0
        self._cloud_retry_at = None

    # -- result handling (service thread) ------------------------------

    def _drain(self, moment):
        while True:
            try:
                result = self._results.get_nowait()
            except queue.Empty:
                return
            self._inflight = False
            self._worker = None
            try:
                self._deliver(result, moment)
            except Exception:
                self._log_event('deliver_failed')

    def _deliver(self, result, moment):
        settings = self._settings()
        # ``tick`` compares instants from ``self.clock`` (monotonic in the
        # service, a fixed value in tests), so the backoff timestamp must use
        # the same base rather than the wall-clock ``moment``.
        instant = self._instant(None)
        cloud = result.get('kind') == 'cloud'
        if cloud:
            # The provider has already charged for this call, so record the
            # usage even if every later gate drops the line.
            self._record_usage(result, settings)
        if not self._speaking_enabled(settings):
            return
        text = None
        if cloud:
            autonomy = self._group(settings, 'autonomy')
            if autonomy.get('cloud_enabled') is not True:
                # The switch was turned off while the request was in flight;
                # the paid result must not be delivered as a cloud message.
                # Fall through to the local phrase path used when cloud is off.
                self._log_event('dropped_cloud_disabled')
            else:
                text = cloud_prompts.validate_text(result.get('text'))
                if text is not None and self._is_repeat(text):
                    text = None
                if text is None:
                    self._log_event('dropped_text')
                    self._note_cloud_failure(instant)
                else:
                    self._note_cloud_success()
        else:
            self._note_cloud_failure(instant)

        policy = self._current_policy(settings, moment)
        if policy is None or not policy['allowed']:
            self._log_event('dropped_policy')
            return
        if not self._budget_allows(settings, policy, moment):
            self._log_event('dropped_budget')
            return

        if text is None:
            self._post_local(moment, policy)
            return
        self._post_cloud(text, moment)

    def _is_repeat(self, text):
        for previous in self._sent:
            if similarity(text, previous) > SIMILARITY_THRESHOLD:
                return True
        return False

    def _record_usage(self, result, settings):
        """Write the provider usage to ``usage_ledger``; never raises."""
        if self.database is None:
            return False
        try:
            ai_settings = self._group(settings, 'ai')
            recorded = self.ledger.record(CLOUD_REQUEST_ID, result.get('model') or '',
                                          result.get('usage') or {}, ai_settings)
        except Exception:
            self._log_event('ledger_failed')
            return False
        if isinstance(recorded, dict) and not recorded.get('recorded'):
            self._log_event('ledger_failed')
            return False
        return True

    def _post_cloud(self, text, moment):
        if self.database is not None:
            try:
                with self.database.transaction() as connection:
                    budget.record_autonomous(connection, now=moment)
            except sqlite3.Error:
                self._log_event('record_failed')
                return
        self._post(text, '', 'cloud', moment)

    def _post_local(self, moment, policy):
        if self.database is None:
            return
        if not self._phrases_enabled(self._settings()):
            # The phrase switch belongs to the local fallback only. Without a
            # phrase library (or with phrases off) a failed cloud call simply
            # stays silent.
            self._log_event('skipped_phrases_disabled')
            return
        categories = policy.get('categories') or []
        # A policy may permit several intents (the gentle idle policy allows
        # care, idle and night). Passing the whole list keeps every permitted
        # kind reachable; an empty list means "any category", as before.
        category = categories or None
        try:
            with self.database.transaction() as connection:
                phrase = phrases.select_phrase(connection, category=category,
                                               now=moment, rng=self.rng)
                if phrase is None:
                    return
                budget.record_autonomous(connection, now=moment)
        except (OSError, ValueError, UnicodeError, sqlite3.Error):
            return
        self._post(phrase['text'], phrase['id'], 'local', moment)

    def _post(self, text, phrase_id, source, moment):
        self._sent.append(text)
        self._record_history(text)
        payload = {
            'text': text,
            'phrase_id': phrase_id,
            'source': source,
            'created_at': moment.astimezone(timezone.utc).isoformat(),
        }
        self.runtime.post(CLOUD_REQUEST_ID, CLOUD_EVENT, payload)
        self._log_event('posted', 'source=' + source)

    def _record_history(self, text):
        """Append the delivered line to the shared chat history.

        History is context, not delivery: a storage failure only records an
        event and the bubble is still posted.
        """
        if self.database is None or type(text) is not str or not text.strip():
            return
        try:
            from service.features import proactive_store
            stored = proactive_store.append_assistant(self.database, text)
        except Exception:
            stored = None
        if stored is None:
            self._log_event('history_failed')

    def _log_event(self, code, message=''):
        """Record one proactive event in the runtime's diagnostic event log.

        The service has no logging handler configured, so ``_LOGGER.info``
        alone would be invisible in stderr and in the diagnostic bundle.
        Event codes only; message text, keys and window titles never pass here.
        """
        events = getattr(self.runtime, 'events', None)
        record = getattr(events, 'record', None)
        if callable(record):
            try:
                record('info', 'proactive', code, message)
                return
            except Exception:
                pass
        _LOGGER.info('proactive_event=%s %s', code, message)
