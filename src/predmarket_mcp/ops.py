"""Operational hardening: metrics, rate limiting, structured logging (B7).

* **Metrics** — a tiny thread-safe counter registry plus a Prometheus-text
  ``/metrics`` endpoint (also folds in circuit-breaker states). Zero deps.
* **Rate limiting** — a per-client token-bucket ASGI middleware. OFF unless
  ``RATE_LIMIT_RPM`` > 0; over-limit callers get a real HTTP ``429`` with
  ``Retry-After``. Keeps one abusive agent from exhausting venue quotas.
* **Structured logging** — JSON log lines when ``LOG_FORMAT=json`` (for log
  aggregation), plain text otherwise.

  RATE_LIMIT_RPM     requests/min per client (default 0 = disabled)
  RATE_LIMIT_BURST   bucket size (default = RPM)
  LOG_FORMAT         text (default) | json
  LOG_LEVEL          INFO (default) | DEBUG | ...
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time


# --- metrics registry -------------------------------------------------------
class Metrics:
    """Process-wide counters. Cheap, thread-safe, label-free (name is enough)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._counters: dict[str, float] = {}

    def inc(self, name: str, amount: float = 1.0) -> None:
        with self._lock:
            self._counters[name] = self._counters.get(name, 0.0) + amount

    def snapshot(self) -> dict[str, float]:
        with self._lock:
            return dict(self._counters)

    def render_prometheus(self) -> str:
        lines = []
        for name, value in sorted(self.snapshot().items()):
            metric = f"predmarket_{name}"
            lines.append(f"# TYPE {metric} counter")
            lines.append(f"{metric} {value}")
        # Circuit-breaker states as a labeled gauge (0=closed,1=half_open,2=open).
        try:
            from core.circuit import breaker_states

            states = {"closed": 0, "half_open": 1, "open": 2}
            for host, state in breaker_states().items():
                lines.append("# TYPE predmarket_circuit_state gauge")
                lines.append(f'predmarket_circuit_state{{host="{host}"}} {states.get(state, 0)}')
        except Exception:
            pass
        return "\n".join(lines) + "\n"


METRICS = Metrics()


# --- rate limiter (token bucket) --------------------------------------------
class TokenBucket:
    def __init__(self, rate_per_sec: float, capacity: float):
        self.rate = rate_per_sec
        self.capacity = capacity
        self.tokens = capacity
        self.updated = time.monotonic()

    def take(self, n: float = 1.0) -> bool:
        now = time.monotonic()
        self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
        self.updated = now
        if self.tokens >= n:
            self.tokens -= n
            return True
        return False


class RateLimiter:
    # Bound the per-client bucket map so many distinct callers can't grow
    # memory without limit; oldest-touched buckets are evicted (their next
    # request just starts a fresh full bucket — safe for a limiter).
    MAX_BUCKETS = int(os.getenv("RATE_LIMIT_MAX_CLIENTS", "10000"))

    def __init__(self, rpm: int, burst: int | None = None):
        from collections import OrderedDict

        self.rpm = rpm
        self.rate = rpm / 60.0
        self.capacity = float(burst if burst is not None else rpm)
        self._buckets: "OrderedDict[str, TokenBucket]" = OrderedDict()
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self.rpm > 0

    def allow(self, client_id: str) -> bool:
        if not self.enabled:
            return True
        with self._lock:
            bucket = self._buckets.get(client_id)
            if bucket is None:
                bucket = TokenBucket(self.rate, self.capacity)
                self._buckets[client_id] = bucket
            self._buckets.move_to_end(client_id)
            while len(self._buckets) > self.MAX_BUCKETS:
                self._buckets.popitem(last=False)
            return bucket.take()


def _rate_limiter() -> RateLimiter:
    rpm = int(os.getenv("RATE_LIMIT_RPM", "0"))
    burst = os.getenv("RATE_LIMIT_BURST")
    return RateLimiter(rpm, int(burst) if burst else None)


class RateLimitMiddleware:
    """ASGI middleware: token-bucket per client, 429 on exceed. Inert if RPM<=0."""

    def __init__(self, app):
        self.app = app
        self.limiter = _rate_limiter()

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or not self.limiter.enabled:
            return await self.app(scope, receive, send)
        METRICS.inc("http_requests_total")
        client_id = self._client_id(scope)
        if not self.limiter.allow(client_id):
            METRICS.inc("rate_limited_total")
            await self._send_429(send)
            return
        return await self.app(scope, receive, send)

    @staticmethod
    def _client_id(scope) -> str:
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        from .billing.middleware import client_fingerprint

        cid = client_fingerprint(headers)
        if cid:
            return cid
        client = scope.get("client")
        return client[0] if client else "anon"

    @staticmethod
    async def _send_429(send):
        body = json.dumps({"error": "rate_limited", "detail": "too many requests"}).encode()
        await send({
            "type": "http.response.start",
            "status": 429,
            "headers": [
                (b"content-type", b"application/json"),
                (b"retry-after", b"1"),
                (b"content-length", str(len(body)).encode()),
            ],
        })
        await send({"type": "http.response.body", "body": body})


# --- structured logging -----------------------------------------------------
class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def configure_logging() -> None:
    """Set up root logging honoring LOG_FORMAT / LOG_LEVEL (idempotent)."""
    level = getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO)
    handler = logging.StreamHandler()
    if os.getenv("LOG_FORMAT", "text").lower() == "json":
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
