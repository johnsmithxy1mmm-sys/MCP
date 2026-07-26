"""Watch / alert subscription tests."""

from __future__ import annotations

import pytest

from core.models import Leg, Opportunity, OpportunityKind, Side, Venue
from core.watches import WatchStore


def _opp(edge, title="btc arb", kind=OpportunityKind.CROSS_VENUE, category="crypto"):
    return Opportunity(
        kind=kind, title=title, category=category, realizable_edge=edge, max_size_usd=1000,
        legs=[Leg(venue=Venue.POLYMARKET, market_id="pm", side=Side.YES)],
    )


def _store(tmp_path):
    return WatchStore(f"sqlite:///{tmp_path / 'w.db'}")


def test_watch_fires_above_threshold_only(tmp_path):
    store = _store(tmp_path)
    store.create_watch("agent-1", min_edge=0.03)
    fired = store.fire([_opp(0.05), _opp(0.01, title="small")])  # only the 0.05 crosses
    assert fired == 1
    alerts = store.drain("agent-1")
    assert len(alerts) == 1 and alerts[0]["opportunity"]["realizable_edge"] == 0.05


def test_alerts_dedupe_and_deliver_once(tmp_path):
    store = _store(tmp_path)
    store.create_watch("agent-1", min_edge=0.02)
    store.fire([_opp(0.05)])
    store.fire([_opp(0.05)])          # same standing opportunity -> no new alert
    first = store.drain("agent-1")
    second = store.drain("agent-1")   # already delivered
    assert len(first) == 1 and second == []


def test_watch_filters_by_kind_and_event(tmp_path):
    store = _store(tmp_path)
    store.create_watch("agent-1", min_edge=0.0, kind="dutch_book", event="president")
    store.fire([
        _opp(0.05, title="btc arb", kind=OpportunityKind.CROSS_VENUE),        # wrong kind
        _opp(0.05, title="2028 president dutch book", kind=OpportunityKind.DUTCH_BOOK),  # matches
    ])
    alerts = store.drain("agent-1")
    assert len(alerts) == 1 and "president" in alerts[0]["opportunity"]["title"].lower()


def test_alerts_are_per_client(tmp_path):
    store = _store(tmp_path)
    store.create_watch("agent-1", min_edge=0.0)
    store.create_watch("agent-2", min_edge=0.99)  # never fires
    store.fire([_opp(0.05)])
    assert len(store.drain("agent-1")) == 1
    assert store.drain("agent-2") == []


def test_alert_queue_is_capped_per_client(tmp_path, monkeypatch):
    # A client that never polls keeps only the newest ALERT_QUEUE_MAX alerts
    # (mirrors the Redis LTRIM path) — unbounded queues would grow the DB forever.
    import core.watches as w

    monkeypatch.setattr(w, "ALERT_QUEUE_MAX", 5)
    store = _store(tmp_path)
    store.create_watch("agent-1", min_edge=0.0)
    store.fire([_opp(0.05, title=f"opp-{i}") for i in range(12)])
    alerts = store.drain("agent-1")
    assert len(alerts) == 5
    # Delivered alerts are not clawed back by the cap on later fires.
    store.fire([_opp(0.05, title=f"late-{i}") for i in range(3)])
    assert len(store.drain("agent-1")) == 3


# --- end-to-end through the tools -------------------------------------------
@pytest.mark.asyncio
async def test_watch_then_poll_alerts_tool_flow(client):
    # Register a broad watch; the mock seed has opportunities above 0.
    # Registration runs an immediate scan and returns standing matches inline.
    reg = (await client.call_tool("watch", {"min_edge": 0.01})).data
    assert reg["watch_id"].startswith("w_")
    assert reg["tier"] == "paid"
    assert len(reg["immediate_matches"]) >= 1

    # Those were delivered with registration, so a follow-up poll has nothing new.
    follow = (await client.call_tool("poll_alerts", {})).data
    assert follow["count"] == 0


def test_watch_cap_holds_under_concurrency(tmp_path, monkeypatch):
    """INV-009: count_active() отпускал лок до вставки, и параллельные запросы
    все проходили проверку — лимит перепрыгивался. Каждый watch оценивается на
    каждом fire, поэтому это управляемая клиентом нагрузка на каждый скан."""
    import threading
    import core.watches as w

    monkeypatch.setattr(w, "WATCH_MAX_PER_CLIENT", 5)
    store = _store(tmp_path)
    barrier = threading.Barrier(20)

    def worker():
        barrier.wait()
        try:
            store.create_watch("greedy", 0.01)
        except w.WatchLimitError:
            pass

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert store.count_active("greedy") <= 5
