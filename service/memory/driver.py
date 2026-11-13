"""Background driver for model-assisted memory compaction.

``jobs.tick`` runs on the service thread and must not block it with a network
call. This driver takes the model-compaction jobs out of that path: the service
thread selects the job and commits the result, while only the provider call runs
on the worker thread owned by :class:`service.background.TaskRunner`.

``TaskRunner.poll`` executes callbacks on the calling (service) thread, so every
SQLite write below stays where the rest of the service expects it.
"""

from datetime import datetime, timezone
import time

from service.background import TaskRunner
from service.memory import compact, jobs

# The runtime ticks every 100 ms; re-reading settings and scanning the queue at
# that rate is wasted work for a maintenance task. One check every few seconds
# keeps latency at the queue's own scheduling granularity.
CHECK_SECONDS = 5.0
JOIN_TIMEOUT_SECONDS = 0.5


def _moment():
    """Wall-clock now.

    Runtime tick hooks pass a monotonic float, which cannot be mapped onto the
    stored timestamps; jobs.py makes the same choice (see its ``_moment``).
    """
    return datetime.now(timezone.utc)


def _settings(store):
    try:
        value = store.settings
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


class ModelCompactDriver:
    """At most one model compaction in flight, driven from a tick hook.

    Two stores are needed and they are not interchangeable: ``settings_store``
    supplies ``.settings`` and the secret store for the provider, while
    ``memory_store`` owns the SQLite connection every ``jobs.*`` helper uses.
    """

    NAME = 'memory-compact'

    def __init__(self, settings_store, memory_store, runner=None, clock=None):
        self.settings_store = settings_store
        self.memory_store = memory_store
        self.runner = runner if runner is not None else TaskRunner(self.NAME)
        # Monotonic by default: this only paces the check, never storage.
        self.clock = clock if callable(clock) else time.monotonic
        self._closed = False
        self._last_check = None

    def busy(self):
        return self.runner.busy()

    def tick(self, now=None):
        """Drain finished work, then start at most one model compaction."""
        if self._closed:
            return
        # Callbacks run here, on the service thread; drain them before deciding
        # whether a new job may start.
        self.runner.poll()
        if self._closed or self.runner.busy():
            return
        instant = self.clock()
        if self._last_check is not None and instant - self._last_check < CHECK_SECONDS:
            return
        self._last_check = instant

        settings = _settings(self.settings_store)
        moment = _moment()
        if jobs.is_paused(self.memory_store):
            return
        if not jobs.should_run(self.memory_store, settings, moment,
                               jobs.counts(self.memory_store)['pending']):
            return
        job = jobs.next_model_job(self.memory_store, settings, moment)
        if job is None:
            return
        self._start(job, settings, moment)

    def close(self, timeout=JOIN_TIMEOUT_SECONDS):
        """Stop accepting work and join the worker within a bounded time."""
        if self._closed:
            return
        self._closed = True
        self.runner.close(timeout)

    # -- internals -------------------------------------------------------

    def _start(self, job, settings, moment):
        """Prepare input on this thread, then hand the network call to the worker."""
        prepared = jobs.prepare_model_job(self.memory_store, job, settings, moment)
        if 'skip' in prepared:
            # next_model_job already marked the job running; complete it so it
            # does not linger until _recover_stale in a later tick.
            jobs.finish_model_job(self.memory_store, job, moment, prepared['skip'])
            return
        ai_settings = settings.get('ai') if isinstance(settings.get('ai'), dict) else {}
        # Building the compressor reads the API key here, on the service thread;
        # the returned callable performs the network request on the worker.
        compressor = compact.build_compressor(self.settings_store, ai_settings)
        if compressor is None:
            jobs.retry_model_job(self.memory_store, job, moment,
                                 RuntimeError('compressor_unavailable'))
            return
        text = prepared['text']
        target = prepared['target']

        def work(post):
            return compressor(text, target)

        def done(raw):
            if self._closed:
                return
            when = _moment()
            try:
                result = jobs.complete_model_job(self.memory_store, job, settings,
                                                 when, raw)
            except Exception as exc:  # noqa: BLE001 - any failure is a retry
                jobs.retry_model_job(self.memory_store, job, when, exc)
            else:
                jobs.finish_model_job(self.memory_store, job, when, result)

        def failed(exc):
            jobs.retry_model_job(self.memory_store, job, _moment(), exc)

        if not self.runner.submit(job['id'], work, done, failed):
            # A lost race with another submitter must not strand a running job.
            jobs.retry_model_job(self.memory_store, job, moment,
                                 RuntimeError('worker_unavailable'))
