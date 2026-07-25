"""D1: Redis-backed shared replay-protection + rate limiting.

Uses a self-contained in-memory fake Redis (no dependency), exercising the same
client surface the real redis-py exposes, so the shared-backend code paths are
tested without infra.
"""

from __future__ import annotations

import fnmatch


from predmarket_mcp.billing.x402 import NonceStore, payment_fingerprint
from predmarket_mcp.ops import RateLimiter


class FakeRedis:
    """Minimal in-memory Redis stand-in (single 'instance')."""

    def __init__(self):
        self.store: dict[str, str] = {}

    def ping(self):
        return True

    def exists(self, key):
        return 1 if key in self.store else 0

    def set(self, key, val, nx=False, ex=None):
        if nx and key in self.store:
            return None            # already set -> NX fails (matches redis-py)
        self.store[key] = val
        return True

    def scan_iter(self, match="*"):
        for k in list(self.store):
            if fnmatch.fnmatch(k, match):
                yield k

    def incr(self, key):
        self.store[key] = str(int(self.store.get(key, "0")) + 1)
        return int(self.store[key])

    def expire(self, key, seconds):
        return True

    def delete(self, key):
        self.store.pop(key, None)


def _payment(sig="0xsig", nonce="n1"):
    return {"payload": {"signature": sig, "authorization": {"from": "a", "to": "b", "nonce": nonce}}}


# --- replay protection across instances -------------------------------------
def test_nonce_store_redis_rejects_replay(tmp_path):
    redis = FakeRedis()
    store = NonceStore(f"sqlite:///{tmp_path / 'x.db'}", redis_client=redis)
    fp = payment_fingerprint(_payment())

    assert store.is_used(fp) is False
    assert store.mark_used(fp) is True     # first consumption accepted
    assert store.is_used(fp) is True
    assert store.mark_used(fp) is False    # replay rejected (SET NX fails)
    assert store.count() == 1


def test_two_instances_share_one_redis(tmp_path):
    redis = FakeRedis()  # the shared backend both instances point at
    a = NonceStore(f"sqlite:///{tmp_path / 'a.db'}", redis_client=redis)
    b = NonceStore(f"sqlite:///{tmp_path / 'b.db'}", redis_client=redis)
    fp = payment_fingerprint(_payment())

    assert a.mark_used(fp) is True         # consumed on instance A
    # Instance B sees it immediately — the same X-PAYMENT can't buy a second call.
    assert b.is_used(fp) is True
    assert b.mark_used(fp) is False


def test_nonce_store_falls_back_to_sqlite(tmp_path):
    store = NonceStore(f"sqlite:///{tmp_path / 'x.db'}", redis_client=None)
    fp = payment_fingerprint(_payment())
    assert store.mark_used(fp) is True
    assert store.mark_used(fp) is False    # sqlite path still rejects replays


# --- shared rate limiting ---------------------------------------------------
def test_rate_limiter_redis_is_global():
    redis = FakeRedis()
    # Two "instances" sharing one Redis; combined they must not exceed rpm.
    a = RateLimiter(rpm=3, redis_client=redis)
    b = RateLimiter(rpm=3, redis_client=redis)
    results = [a.allow("client"), b.allow("client"), a.allow("client"),
               b.allow("client"), a.allow("client")]
    assert results[:3] == [True, True, True]   # first 3 across both instances
    assert results[3] is False and results[4] is False


def test_rate_limiter_redis_degrades_on_error(monkeypatch):
    class BrokenRedis(FakeRedis):
        def incr(self, key):
            raise RuntimeError("redis down")

    rl = RateLimiter(rpm=2, redis_client=BrokenRedis())
    # Redis errors must not block traffic — falls back to the local bucket.
    assert rl.allow("c") is True


# --- kv helper --------------------------------------------------------------
def test_get_redis_none_without_url(monkeypatch):
    from predmarket_mcp import kv

    monkeypatch.delenv("REDIS_URL", raising=False)
    kv.reset_cache()
    assert kv.get_redis() is None
    kv.reset_cache()
