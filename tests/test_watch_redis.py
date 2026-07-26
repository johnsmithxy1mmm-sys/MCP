"""E2: Redis-backed watch store — cross-instance watches + alert delivery."""

from __future__ import annotations


import pytest

from core.models import Leg, Opportunity, OpportunityKind, Side, Venue
from core.watches import RedisWatchStore, WatchLimitError


class FakeRedis:
    """In-memory Redis stand-in covering the surface RedisWatchStore uses."""

    def __init__(self):
        self.h: dict[str, dict] = {}
        self.s: dict[str, set] = {}
        self.l: dict[str, list] = {}

    # hashes
    def hset(self, key, field=None, value=None, mapping=None):
        d = self.h.setdefault(key, {})
        if mapping:
            d.update({k: str(v) for k, v in mapping.items()})
        if field is not None:
            d[field] = str(value)
        return 1

    def hgetall(self, key):
        return dict(self.h.get(key, {}))

    # sets
    def sadd(self, key, *members):
        s = self.s.setdefault(key, set())
        added = 0
        for m in members:
            if m not in s:
                s.add(m); added += 1
        return added

    def srem(self, key, *members):
        s = self.s.setdefault(key, set())
        n = 0
        for m in members:
            if m in s:
                s.discard(m); n += 1
        return n

    def smembers(self, key):
        return set(self.s.get(key, set()))

    def scard(self, key):
        return len(self.s.get(key, set()))

    # lists
    def rpush(self, key, *vals):
        self.l.setdefault(key, []).extend(vals)
        return len(self.l[key])

    def lrange(self, key, start, end):
        lst = self.l.get(key, [])
        return lst[start:] if end == -1 else lst[start:end + 1]

    def ltrim(self, key, start, end):
        lst = self.l.get(key, [])
        stop = len(lst) if end == -1 else end + 1
        begin = max(0, len(lst) + start) if start < 0 else start
        self.l[key] = lst[begin:stop]
        return True

    def expire(self, key, seconds):
        self.expired = getattr(self, "expired", {})
        self.expired[key] = seconds
        return True

    def delete(self, key):
        existed = key in self.l or key in self.h or key in self.s
        self.l.pop(key, None)
        self.h.pop(key, None)
        self.s.pop(key, None)
        return 1 if existed else 0

    def pipeline(self, transaction=True):
        return _FakePipeline(self)


class _FakePipeline:
    def __init__(self, r):
        self.r = r
        self.ops = []

    def lrange(self, k, s, e):
        self.ops.append(("lrange", (k, s, e))); return self

    def delete(self, k):
        self.ops.append(("delete", (k,))); return self

    def execute(self):
        return [getattr(self.r, op)(*args) for op, args in self.ops]


def _opp(edge=0.05, title="btc bundle"):
    return Opportunity(kind=OpportunityKind.BUNDLE, title=title, category="crypto",
                       realizable_edge=edge, max_size_usd=100.0,
                       legs=[Leg(venue=Venue.KALSHI, market_id="m1", side=Side.YES)])


def test_create_list_and_count():
    store = RedisWatchStore(FakeRedis())
    wid = store.create_watch("c1", 0.02, category="crypto")
    assert store.count_active("c1") == 1
    watches = store.list_watches("c1")
    assert watches[0]["watch_id"] == wid and watches[0]["min_edge"] == 0.02


def test_watch_limit_enforced(monkeypatch):
    import core.watches as w
    monkeypatch.setattr(w, "WATCH_MAX_PER_CLIENT", 2)
    store = RedisWatchStore(FakeRedis())
    store.create_watch("c1", 0.02)
    store.create_watch("c1", 0.02)
    with pytest.raises(WatchLimitError):
        store.create_watch("c1", 0.02)


def test_cancel_watch():
    store = RedisWatchStore(FakeRedis())
    wid = store.create_watch("c1", 0.02)
    assert store.cancel_watch("c1", wid) is True
    assert store.count_active("c1") == 0
    assert store.cancel_watch("c1", wid) is False  # already gone


def test_fire_dedupes_and_queues(monkeypatch):
    monkeypatch.setattr("core.notifier.notify_alerts", lambda alerts: True)
    store = RedisWatchStore(FakeRedis())
    store.create_watch("c1", 0.01, category="crypto")
    assert store.fire([_opp()]) == 1
    assert store.fire([_opp()]) == 0          # same opp -> deduped
    alerts = store.peek("c1")
    assert len(alerts) == 1 and alerts[0]["opportunity"]["realizable_edge"] == 0.05


def test_two_instances_share_watches_and_alerts(monkeypatch):
    monkeypatch.setattr("core.notifier.notify_alerts", lambda alerts: True)
    redis = FakeRedis()
    a = RedisWatchStore(redis)
    b = RedisWatchStore(redis)
    # Watch created on instance A...
    a.create_watch("c1", 0.01)
    # ...is fired on instance B, and drained on A — true cross-instance push.
    assert b.fire([_opp()]) == 1
    drained = a.drain("c1")
    assert len(drained) == 1
    assert a.peek("c1") == [] and b.peek("c1") == []  # drain consumed it everywhere


def test_peek_does_not_consume():
    store = RedisWatchStore(FakeRedis())
    store.create_watch("c1", 0.01)
    store.fire([_opp()])
    assert len(store.peek("c1")) == 1
    assert len(store.peek("c1")) == 1     # idempotent
    assert len(store.drain("c1")) == 1
    assert store.peek("c1") == []


def test_cancel_cleans_up_all_keys(monkeypatch):
    monkeypatch.setattr("core.notifier.notify_alerts", lambda alerts: True)
    redis = FakeRedis()
    store = RedisWatchStore(redis)
    wid = store.create_watch("c1", 0.01)
    store.fire([_opp()])  # creates the aseen dedup set
    assert store.cancel_watch("c1", wid) is True
    # No leaked hash or dedup set after cancel (random ids are never reused).
    assert redis.h.get(f"w:{wid}") is None
    assert redis.s.get(f"aseen:{wid}") is None


def test_seen_set_gets_a_ttl(monkeypatch):
    monkeypatch.setattr("core.notifier.notify_alerts", lambda alerts: True)
    redis = FakeRedis()
    store = RedisWatchStore(redis)
    wid = store.create_watch("c1", 0.01)
    store.fire([_opp()])
    assert redis.expired.get(f"aseen:{wid}", 0) > 0  # dedup set expires eventually


def test_alert_queue_is_capped(monkeypatch):
    import core.watches as w

    monkeypatch.setattr("core.notifier.notify_alerts", lambda alerts: True)
    monkeypatch.setattr(w, "ALERT_QUEUE_MAX", 3)
    store = RedisWatchStore(FakeRedis())
    store.create_watch("c1", 0.01)
    # 5 distinct opportunities; a never-polling client keeps only the newest 3.
    store.fire([_opp(title=f"opp {i}") for i in range(5)])
    alerts = store.peek("c1")
    assert len(alerts) == 3
    assert alerts[-1]["opportunity"]["title"] == "opp 4"  # newest kept


def test_get_watches_picks_redis_when_available(monkeypatch):
    import core.watches as w
    from predmarket_mcp import kv

    fake = FakeRedis()
    monkeypatch.setattr(kv, "get_redis", lambda: fake)
    w.get_watches.cache_clear()
    store = w.get_watches()
    assert isinstance(store, RedisWatchStore)
    w.get_watches.cache_clear()
