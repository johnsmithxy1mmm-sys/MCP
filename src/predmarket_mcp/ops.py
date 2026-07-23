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
        self._summaries: dict[str, tuple[float, int]] = {}  # name -> (sum, count)

    def inc(self, name: str, amount: float = 1.0) -> None:
        with self._lock:
            self._counters[name] = self._counters.get(name, 0.0) + amount

    def observe(self, name: str, value: float) -> None:
        """Record a sample (e.g. latency ms) into a sum/count summary."""
        with self._lock:
            s, c = self._summaries.get(name, (0.0, 0))
            self._summaries[name] = (s + value, c + 1)

    def snapshot(self) -> dict[str, float]:
        with self._lock:
            return dict(self._counters)

    def render_prometheus(self) -> str:
        lines = []
        for name, value in sorted(self.snapshot().items()):
            metric = f"predmarket_{name}"
            lines.append(f"# TYPE {metric} counter")
            lines.append(f"{metric} {value}")
        with self._lock:
            summaries = dict(self._summaries)
        for name, (total, count) in sorted(summaries.items()):
            metric = f"predmarket_{name}"
            lines.append(f"# TYPE {metric} summary")
            lines.append(f"{metric}_sum {round(total, 4)}")
            lines.append(f"{metric}_count {count}")
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

    def __init__(self, rpm: int, burst: int | None = None, redis_client=None):
        from collections import OrderedDict

        self.rpm = rpm
        self.rate = rpm / 60.0
        self.capacity = float(burst if burst is not None else rpm)
        self._buckets: "OrderedDict[str, TokenBucket]" = OrderedDict()
        self._lock = threading.Lock()
        # Shared limiter across instances when Redis is configured (a per-process
        # bucket would let a caller do rpm x instances). Fixed-window counter.
        if redis_client is not None:
            self._redis = redis_client
        else:
            try:
                from .kv import get_redis
                self._redis = get_redis()
            except Exception:
                self._redis = None

    @property
    def enabled(self) -> bool:
        return self.rpm > 0

    def allow(self, client_id: str) -> bool:
        if not self.enabled:
            return True
        if self._redis is not None:
            return self._allow_redis(client_id, self.rpm)
        return self._allow_local(client_id, self.rate, self.capacity)

    def allow_ip(self, ip: str) -> bool:
        """Secondary per-IP cap at ``rpm x RATE_LIMIT_IP_MULT`` (default 5x).

        The per-client key comes from spoofable headers, so rotating
        ``x-client-id`` values would otherwise sidestep the limit entirely. The
        IP cap bounds total throughput per source address regardless of how many
        client ids it invents. Set RATE_LIMIT_IP_MULT=0 to disable (e.g. when
        many legit tenants share one egress IP).
        """
        mult = int(os.getenv("RATE_LIMIT_IP_MULT", "5"))
        if not self.enabled or mult <= 0 or not ip:
            return True
        limit = self.rpm * mult
        if self._redis is not None:
            return self._allow_redis(f"ip:{ip}", limit)
        return self._allow_local(f"ip:{ip}", limit / 60.0, float(limit))

    def _allow_redis(self, key_id: str, limit: int) -> bool:
        """Global fixed-window counter: `limit` requests per 60s across instances."""
        import time as _t

        window = int(_t.time() // 60)
        key = f"rl:{key_id}:{window}"
        try:
            count = self._redis.incr(key)
            if count == 1:
                self._redis.expire(key, 60)
            return count <= limit
        except Exception:  # Redis hiccup -> degrade, don't block
            return self._allow_local(key_id, limit / 60.0, float(limit))

    def _allow_local(self, key_id: str, rate: float, capacity: float) -> bool:
        with self._lock:
            bucket = self._buckets.get(key_id)
            if bucket is None:
                bucket = TokenBucket(rate, capacity)
                self._buckets[key_id] = bucket
            self._buckets.move_to_end(key_id)
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
        client_id, ip = self._client_id_and_ip(scope)
        # Per-client bucket first; then the per-IP cap, which holds even when the
        # caller rotates spoofable x-client-id values to mint fresh buckets.
        if not self.limiter.allow(client_id) or not self.limiter.allow_ip(ip):
            METRICS.inc("rate_limited_total")
            await self._send_429(send)
            return
        return await self.app(scope, receive, send)

    @staticmethod
    def _client_id_and_ip(scope) -> tuple[str, str]:
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        from .billing.middleware import client_fingerprint

        client = scope.get("client")
        peer = client[0] if client else ""
        # Behind a proxy the peer is the LB; prefer the forwarded client address.
        ip = headers.get("x-forwarded-for", "").split(",")[0].strip() or peer
        cid = client_fingerprint(headers)
        return (cid or ip or "anon"), ip

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
