"""Regression tests for the P0 hardening pass (A6-A9).

Cache eviction bounds, venue unit correctness, watch limits + the background
alert engine, and the shared risk-finalization (earliest close across legs,
depth-capped size).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core import algorithms
from core.models import (
    Leg,
    Market,
    Opportunity,
    OpportunityKind,
    OrderbookLevel,
    OrderbookSnapshot,
    Side,
    Venue,
)


# --- A6: bounded caches -----------------------------------------------------
def test_embed_cache_is_bounded(monkeypatch):
    from core import embeddings

    monkeypatch.setattr(embeddings, "_CACHE_SIZE", 3)
    embeddings.clear_cache()
    for i in range(10):
        embeddings._cache_put(f"t{i}", [float(i)])
    assert len(embeddings._CACHE) == 3  # never grows past the bound
    # LRU: the three most recently inserted survive.
    assert embeddings._cache_get("t9") == [9.0]
    assert embeddings._cache_get("t0") is None  # evicted


def test_live_ttl_cache_is_bounded():
    from core.live import _TTLCache

    cache = _TTLCache(ttl=60, max_entries=2)
    cache.put("a", 1)
    cache.put("b", 2)
    cache.put("c", 3)  # evicts LRU ("a")
    assert cache.get("a") is None
    assert cache.get("c") == 3


# --- A7: Kalshi volume/size are USD notional, not contract counts -----------
def test_kalshi_volume_is_usd_notional():
    from core.adapters.kalshi import KalshiAdapter

    row = {"ticker": "KX-1", "title": "t", "yes_bid": 40, "yes_ask": 40, "volume": 1000}
    m = KalshiAdapter()._normalize_market(row)
    # 1000 contracts * $0.40 = $400 notional (not 1000 "dollars").
    assert m.yes_price == 0.4
    assert m.volume_usd == 400.0


# --- A8: watch limit + background alert engine ------------------------------
def test_watch_limit_enforced(tmp_path):
    from core.watches import WatchLimitError, WatchStore
    import core.watches as w

    store = WatchStore(db_url=f"sqlite:///{tmp_path / 'w.db'}")
    limit = 3
    orig = w.WATCH_MAX_PER_CLIENT
    w.WATCH_MAX_PER_CLIENT = limit
    try:
        for _ in range(limit):
            store.create_watch("client-a", 0.02)
        with pytest.raises(WatchLimitError):
            store.create_watch("client-a", 0.02)
        # A different client is unaffected.
        assert store.create_watch("client-b", 0.02)
    finally:
        w.WATCH_MAX_PER_CLIENT = orig


def test_alert_engine_tick_fires_watches(tmp_path):
    from core.watches import AlertEngine, WatchStore

    store = WatchStore(db_url=f"sqlite:///{tmp_path / 'w.db'}")
    store.create_watch("c1", 0.01)
    opp = Opportunity(
        kind=OpportunityKind.BUNDLE, title="x", category=None,
        realizable_edge=0.05, max_size_usd=100.0,
        legs=[Leg(venue=Venue.KALSHI, market_id="m1", side=Side.YES)],
    )
    engine = AlertEngine(store, scan_fn=lambda: [opp], interval=0.01)
    assert engine.tick() == 1  # one new alert queued
    assert engine.tick() == 0  # deduped on the second cycle
    assert store.drain("c1")

    # A scan that raises must not propagate (loop keeps running).
    def boom():
        raise RuntimeError("scan down")

    assert AlertEngine(store, scan_fn=boom).tick() == 0


# --- A9: risk finalization (earliest close across legs, depth cap) ----------
def _mk(mid, close_days):
    return Market(
        venue=Venue.KALSHI, market_id=mid, title=mid, yes_price=0.5, no_price=0.5,
        close_time=datetime.now(timezone.utc) + timedelta(days=close_days),
    )


def test_annotate_uses_earliest_close_across_all_legs():
    opp = Opportunity(
        kind=OpportunityKind.CROSS_VENUE, title="x", category=None,
        realizable_edge=0.10, max_size_usd=100.0,
        legs=[
            Leg(venue=Venue.KALSHI, market_id="near", side=Side.YES),
            Leg(venue=Venue.POLYMARKET, market_id="far", side=Side.NO),
        ],
    )
    markets = [_mk("far", 100), _mk("near", 2)]  # 'far' is leg[0]-agnostic
    algorithms.finalize_opportunities([opp], markets)
    # Holding period must reflect the NEAREST close (2 days), not leg[0]/far.
    assert opp.holding_days == pytest.approx(2.0, abs=0.05)


def test_finalize_caps_size_by_book_depth():
    opp = Opportunity(
        kind=OpportunityKind.BUNDLE, title="x", category=None,
        realizable_edge=0.05, max_size_usd=10_000.0,  # optimistic volume-based cap
        legs=[Leg(venue=Venue.KALSHI, market_id="m", side=Side.YES)],
    )

    def book_getter(venue, market_id):
        # YES leg fills against asks; total ask depth = 120.
        return OrderbookSnapshot(
            venue=Venue.KALSHI, market_id=market_id, as_of=datetime.now(timezone.utc),
            yes_asks=[OrderbookLevel(price=0.5, size_usd=120.0)], yes_bids=[],
        )

    algorithms.finalize_opportunities([opp], [], book_getter)
    assert opp.max_size_usd == 120.0  # shrunk to real depth


def test_finalize_keeps_estimate_when_book_missing():
    opp = Opportunity(
        kind=OpportunityKind.BUNDLE, title="x", category=None,
        realizable_edge=0.05, max_size_usd=500.0,
        legs=[Leg(venue=Venue.KALSHI, market_id="m", side=Side.YES)],
    )
    algorithms.finalize_opportunities([opp], [], lambda v, m: None)
    assert opp.max_size_usd == 500.0  # unverifiable book -> don't overstate/understate
