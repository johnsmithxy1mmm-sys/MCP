"""B1: real-time ingestion — message normalization + history write (offline).

The socket loop needs a network and is flag-gated; here we test the pure tick
normalization and that ingested ticks land in the history store.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from core.storage import HistoryStore
from core.streaming import StreamIngestor, normalize_tick, streaming_enabled


def test_streaming_off_by_default(monkeypatch):
    monkeypatch.delenv("STREAMING", raising=False)
    assert streaming_enabled() is False
    assert StreamIngestor().start() is None  # no thread when disabled


def test_normalize_polymarket_price_change():
    tick = normalize_tick("polymarket", {
        "event_type": "price_change", "market": "0xabc", "price": "0.63",
    })
    assert tick == ("0xabc", 0.63)


def test_normalize_kalshi_ticker_midpoint():
    tick = normalize_tick("kalshi", {
        "type": "ticker",
        "msg": {"market_ticker": "KX-1", "yes_bid": 40, "yes_ask": 44},
    })
    assert tick == ("KX-1", 0.42)  # midpoint of 40c/44c


def test_normalize_ignores_non_price_messages():
    assert normalize_tick("polymarket", {"event_type": "subscribed"}) is None
    assert normalize_tick("kalshi", {"type": "heartbeat"}) is None
    assert normalize_tick("unknown", {"foo": 1}) is None


def test_on_tick_records_to_history(tmp_path):
    hist = HistoryStore(db_url=f"sqlite:///{tmp_path / 'h.db'}")
    ing = StreamIngestor(history=hist)

    assert ing.on_tick("kalshi", {
        "type": "ticker", "msg": {"market_ticker": "KX-1", "yes_bid": 50, "yes_ask": 50},
    }) is True
    assert ing.on_tick("kalshi", {"type": "heartbeat"}) is False  # skipped

    now = datetime.now(timezone.utc)
    pts = hist.query("kalshi", "KX-1", now - timedelta(minutes=1), now + timedelta(minutes=1))
    assert pts and pts[-1].yes_price == 0.5


def test_on_tick_feeds_sink(tmp_path):
    seen = []
    ing = StreamIngestor(
        history=HistoryStore(db_url=f"sqlite:///{tmp_path / 'h.db'}"),
        sink=lambda v, m, p: seen.append((v, m, p)),
    )
    ing.on_tick("polymarket", {"event_type": "price_change", "market": "0xabc", "price": 0.7})
    assert seen == [("polymarket", "0xabc", 0.7)]
