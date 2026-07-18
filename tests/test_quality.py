"""F7: quote-quality guard. F8: term-structure in the event graph."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from core.quality import is_usable, quote_quality
from core.eventgraph import term_structures
from core.models import Market, OrderbookLevel, OrderbookSnapshot, Venue


# --- F7 quality -------------------------------------------------------------
def test_healthy_quote_scores_high():
    q = quote_quality(0.6, volume_usd=100_000, data_age_seconds=5)
    assert q["score"] >= 0.9 and q["usable"] and q["flags"] == []


def test_stale_thin_flags():
    q = quote_quality(0.6, volume_usd=100, data_age_seconds=9999)
    assert "stale" in q["flags"] and "thin" in q["flags"]
    assert q["score"] < 0.6


def test_crossed_book_flagged():
    book = OrderbookSnapshot(
        venue=Venue.KALSHI, market_id="m", as_of=datetime.now(timezone.utc),
        yes_bids=[OrderbookLevel(price=0.60, size_usd=100)],
        yes_asks=[OrderbookLevel(price=0.55, size_usd=100)],  # ask < bid = crossed
    )
    q = quote_quality(0.58, 100_000, 5, orderbook=book)
    assert "crossed_book" in q["flags"]


def test_gate_off_by_default(monkeypatch):
    monkeypatch.delenv("QUALITY_GATE", raising=False)
    thin = Market(venue=Venue.KALSHI, market_id="m", title="t",
                  yes_price=0.5, no_price=0.5, volume_usd=1.0)
    assert is_usable(thin) is True  # advisory unless the gate is on


def test_gate_on_filters_thin(monkeypatch):
    monkeypatch.setenv("QUALITY_GATE", "on")
    monkeypatch.setenv("QUALITY_MIN", "0.85")
    thin = Market(venue=Venue.KALSHI, market_id="m", title="t",
                  yes_price=0.5, no_price=0.5, volume_usd=1.0)
    healthy = Market(venue=Venue.KALSHI, market_id="m2", title="t",
                     yes_price=0.5, no_price=0.5, volume_usd=500_000)
    assert is_usable(thin) is False and is_usable(healthy) is True


@pytest.mark.asyncio
async def test_evaluate_market_carries_quality(client):
    r = (await client.call_tool(
        "evaluate_market", {"venue": "polymarket", "market_id": "pm-btc-100k-2026"}
    )).data
    assert "quality" in r and "score" in r["quality"]


# --- F8 term structure ------------------------------------------------------
def _m(title, yes, mid):
    return Market(venue=Venue.POLYMARKET, market_id=mid, title=title, yes_price=yes,
                  no_price=round(1 - yes, 4), category="crypto")


def test_term_structure_curve_and_monotonicity():
    markets = [
        _m("Will Bitcoin reach $200k by end of 2026?", 0.30, "a"),
        _m("Will Bitcoin reach $200k by end of 2027?", 0.45, "b"),
        _m("Will Bitcoin reach $200k by end of 2028?", 0.55, "c"),
    ]
    ts = term_structures(markets)
    assert len(ts) == 1
    curve = ts[0]["curve"]
    assert [pt["probability"] for pt in curve] == [0.30, 0.45, 0.55]
    assert ts[0]["monotone"] is True  # rising with the deadline = well-formed


def test_term_structure_flags_non_monotone():
    markets = [
        _m("Will Bitcoin reach $200k by end of 2026?", 0.50, "a"),
        _m("Will Bitcoin reach $200k by end of 2027?", 0.40, "b"),  # dips -> broken
    ]
    ts = term_structures(markets)
    assert ts[0]["monotone"] is False
