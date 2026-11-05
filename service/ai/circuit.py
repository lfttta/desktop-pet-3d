"""Consecutive infrastructure-failure tracking with a cooldown circuit.

Only rate limits, timeouts, connection failures and server errors count toward
opening the circuit. After the cooldown a single trial request is allowed; a
success closes the circuit and a failure reopens it for another cooldown.
"""

import threading
import time

INFRASTRUCTURE_CODES = frozenset({
    'rate_limited', 'timeout', 'connection_error', 'server_error',
})


class CircuitBreaker:
    def __init__(self, threshold=3, cooldown_seconds=60.0, clock=None):
        self.threshold = max(1, int(threshold))
        self.cooldown_seconds = max(0.0, float(cooldown_seconds))
        self._clock = clock or time.monotonic
        self._failure_count = 0
        self._open_until = 0.0
        self._half_open = False
        self._lock = threading.Lock()

    def allow(self):
        """Return True when a request may run; the cooldown opens one trial."""
        with self._lock:
            now = self._clock()
            if self._open_until <= 0.0:
                return True
            if now < self._open_until:
                return False
            self._half_open = True
            return True

    def is_open(self):
        """Report whether the cooldown is still blocking new requests."""
        with self._lock:
            return self._open_until > 0.0 and self._clock() < self._open_until

    def record_success(self):
        with self._lock:
            self._failure_count = 0
            self._open_until = 0.0
            self._half_open = False

    def record_failure(self, code):
        """Count one infrastructure failure and return True when it opened."""
        if code not in INFRASTRUCTURE_CODES:
            return False
        with self._lock:
            self._failure_count += 1
            if self._half_open or self._failure_count >= self.threshold:
                self._open_until = self._clock() + self.cooldown_seconds
                self._half_open = False
                return True
            return False

    def snapshot(self):
        with self._lock:
            now = self._clock()
            return {
                'failures': self._failure_count,
                'open': self._open_until > 0.0 and now < self._open_until,
                'half_open': self._half_open,
                'retry_after_seconds': max(0.0, self._open_until - now),
            }
