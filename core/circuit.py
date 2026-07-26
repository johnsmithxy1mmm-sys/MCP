"""Circuit breaker for venue API calls (B7).

A flaky or down venue shouldn't cost every request a full timeout. After
``fail_max`` consecutive failures the breaker OPENS and calls fast-fail for
``reset_timeout`` seconds; then it goes HALF-OPEN and lets one probe through — a
success CLOSES it, a failure re-opens it. This bounds latency and stops a dead
venue from dragging down the whole scan (the live engine already degrades one
venue gracefully; the breaker makes that degradation cheap).

Thread-safe, stdlib only.

  CIRCUIT_FAIL_MAX        consecutive failures before opening (default 5)
  CIRCUIT_RESET_TIMEOUT   seconds open before a half-open probe (default 30)
"""

from __future__ import annotations

import os
import threading
import time


class CircuitOpen(RuntimeError):
    """Raised when a call is short-circuited because the breaker is open."""


class CircuitBreaker:
    def __init__(self, name: str, fail_max: int | None = None, reset_timeout: float | None = None):
        self.name = name
        self.fail_max = fail_max if fail_max is not None else int(os.getenv("CIRCUIT_FAIL_MAX", "5"))
        self.reset_timeout = (
            reset_timeout if reset_timeout is not None
            else float(os.getenv("CIRCUIT_RESET_TIMEOUT", "30"))
        )
        self._lock = threading.Lock()
        self._failures = 0
        self._opened_at = 0.0
        self._state = "closed"  # closed | open | half_open

    @property
    def state(self) -> str:
        with self._lock:
            return self._resolved_state()

    def _resolved_state(self) -> str:
        if self._state == "open" and (time.monotonic() - self._opened_at) >= self.reset_timeout:
            self._state = "half_open"  # cooldown elapsed -> allow one probe
        return self._state

    def allow(self) -> bool:
        """Whether a call may proceed right now."""
        with self._lock:
            return self._resolved_state() in ("closed", "half_open")

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._state = "closed"

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._failures >= self.fail_max or self._state == "half_open":
                self._state = "open"
                self._opened_at = time.monotonic()

    def call(self, fn, *args, ignore: tuple = (), **kwargs):
        """Run ``fn`` through the breaker; raise :class:`CircuitOpen` if open.

        Exceptions listed in ``ignore`` propagate WITHOUT counting as a venue
        failure (e.g. a 404 for a bad market id is the caller's problem, not a
        sign the venue is down — it must not open the breaker).
        """
        if not self.allow():
            raise CircuitOpen(f"circuit '{self.name}' is open")
        try:
            result = fn(*args, **kwargs)
        except ignore:
            raise  # caller error: neither a failure nor a success signal
        except Exception:
            self.record_failure()
            raise
        self.record_success()
        return result


_BREAKERS: dict[str, CircuitBreaker] = {}
_LOCK = threading.Lock()


def get_breaker(name: str) -> CircuitBreaker:
    breaker = _BREAKERS.get(name)
    if breaker is not None:
        return breaker
    with _LOCK:
        if name not in _BREAKERS:
            _BREAKERS[name] = CircuitBreaker(name)
        return _BREAKERS[name]


def breaker_states() -> dict[str, str]:
    """Snapshot of every breaker's state (for /metrics)."""
    return {name: b.state for name, b in _BREAKERS.items()}
