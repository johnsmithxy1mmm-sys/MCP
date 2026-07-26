"""B4: Kelly sizing + market-impact curve."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from core import sizing
from core.models import Leg, OrderbookLevel, OrderbookSnapshot, Side, Venue


def test_kelly_fraction_basic():
    # Fair 0.60, buy YES at 0.50: full Kelly = (0.10)/(0.50) = 0.20; half = 0.10.
    assert sizing.kelly_fraction(0.60, 0.50, fractional=1.0) == 0.20
    assert sizing.kelly_fraction(0.60, 0.50, fractional=0.5) == 0.10


def test_kelly_zero_on_no_edge():
    assert sizing.kelly_fraction(0.50, 0.50) == 0.0   # no edge
    assert sizing.kelly_fraction(0.40, 0.50) == 0.0   # negative edge -> never bet
    assert sizing.kelly_fraction(0.60, 1.0) == 0.0    # degenerate price


def test_kelly_bounded_and_never_exceeds_one():
    # Full Kelly for a YES bet is (q-p)/(1-p) <= 1 for any q<=1; stays in range.
    f = sizing.kelly_fraction(0.99, 0.01, fractional=1.0)
    assert 0.0 <= f <= 1.0 and f == pytest.approx(0.9899, abs=1e-4)


def _book_getter(depth_levels):
    def getter(venue, market_id):
        return OrderbookSnapshot(
            venue=Venue.KALSHI, market_id=market_id, as_of=datetime.now(timezone.utc),
            yes_asks=depth_levels, yes_bids=[],
        )
    return getter


def test_impact_curve_walks_book():
    legs = [Leg(venue=Venue.KALSHI, market_id="m", side=Side.YES)]
    # Cheap shallow level, then a pricier deep level: avg price rises with size.
    getter = _book_getter([
        OrderbookLevel(price=0.50, size_usd=100.0),
        OrderbookLevel(price=0.60, size_usd=10_000.0),
    ])
    curve = sizing.impact_curve(legs, 100.0, getter, multiples=(0.5, 1.0, 4.0))
    prices = [pt["avg_fill_price"] for pt in curve]
    assert prices == sorted(prices)          # impact: avg price non-decreasing
    assert curve[0]["avg_fill_price"] == 0.50  # small size fills at the top level


def test_execution_sizing_includes_kelly_and_curve():
    legs = [Leg(venue=Venue.KALSHI, market_id="m", side=Side.YES)]
    getter = _book_getter([OrderbookLevel(price=0.50, size_usd=100_000.0)])
    out = sizing.execution_sizing(legs, 1000.0, getter, bankroll_usd=10_000.0, fair_value=0.60)
    assert out["kelly_fraction"] == 0.10                    # half-Kelly at fair 0.6 / price 0.5
    assert out["recommended_size_usd"] == 1000.0            # 0.10 * 10_000
    assert out["impact_curve"]


def test_execution_sizing_no_fair_value_skips_kelly():
    legs = [Leg(venue=Venue.KALSHI, market_id="m", side=Side.YES)]
    getter = _book_getter([OrderbookLevel(price=0.50, size_usd=100_000.0)])
    out = sizing.execution_sizing(legs, 1000.0, getter)
    assert out["kelly_fraction"] is None
    assert out["recommended_size_usd"] is None
    assert out["impact_curve"]  # still returned (needs only the book)


@pytest.mark.asyncio
async def test_estimate_execution_tool_returns_sizing(client):
    r = (await client.call_tool(
        "estimate_execution",
        {
            "legs": [{"venue": "polymarket", "market_id": "pm-btc-100k-2026", "side": "yes"}],
            "size_usd": 1000, "bankroll_usd": 50_000, "fair_value": 0.7,
        },
    )).data
    assert "sizing" in r
    assert "impact_curve" in r["sizing"]
    assert r["sizing"]["kelly_fraction"] is not None
