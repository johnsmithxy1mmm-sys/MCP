"""Shared key-value backend (Redis) for horizontal scaling (D1).

Single-instance, the x402 replay guard (NonceStore) and the rate limiter live in
local SQLite / process memory. Behind a load balancer that's wrong two ways:

* **Replay protection must be shared** — a signed payment consumed on instance A
  is invisible to instance B, so the same X-PAYMENT buys N calls across N
  instances. That's lost revenue / double-spend. Redis ``SET NX`` makes
  consumption atomic and global.
* **Rate limits must be shared** — a per-process bucket lets a caller do
  ``limit x instances`` requests. A global Redis counter enforces the real limit.

OFF by default: no ``REDIS_URL`` (or ``redis`` not installed) → callers keep their
local backend. The client is created lazily so the dependency is optional.

  REDIS_URL   redis://host:6379/0 — enable the shared backend
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache

log = logging.getLogger("predmarket.kv")


def redis_from_url(url: str):
    """Build a Redis client from a URL, or None if the driver is unavailable."""
    try:
        import redis  # optional dependency
    except ImportError:
        log.warning("REDIS_URL set but `redis` not installed; using local backend")
        return None
    try:
        client = redis.Redis.from_url(url, decode_responses=True)
        client.ping()
        return client
    except Exception as exc:  # unreachable / auth / etc.
        log.warning("Redis unavailable (%s); using local backend", type(exc).__name__)
        return None


@lru_cache(maxsize=1)
def get_redis():
    """Process-cached Redis client from ``REDIS_URL``, or None when unset/unavailable."""
    url = os.getenv("REDIS_URL")
    if not url:
        return None
    return redis_from_url(url)


def reset_cache() -> None:
    """Drop the cached client (tests / after a config change)."""
    get_redis.cache_clear()
