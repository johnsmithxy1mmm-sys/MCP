"""B3: fair-value / consensus engine."""

from __future__ import annotations

import pytest

from core import fairvalue
from core.models import Market, Venue


def _m(venue, yes, vol):
    return Market(venue=venue, market_id=f"{venue.value}-x", title="btc 100k",
                  yes_price=yes, no_price=round(1 - yes, 4), volume_usd=vol)


def test_consensus_is_liquidity_weighted():
    # Kalshi (deep) at 0.60, Polymarket (thin) at 0.70 -> consensus near 0.60.
    markets = [_m(Venue.KALSHI, 0.60, 900_000), _m(Venue.POLYMARKET, 0.70, 100_000)]
    fair = fairvalue.consensus(markets)
    assert 0.60 <= fair <= 0.62  # pulled toward the deeper venue


def test_consensus_none_when_empty():
    assert fairvalue.consensus([]) is None


def test_assess_reports_deviation_and_confidence():
    markets = [_m(Venue.KALSHI, 0.50, 100), _m(Venue.POLYMARKET, 0.50, 100)]
    a = fairvalue.assess(markets)
    assert a["fair_value"] == 0.5
    assert a["confidence"] == 1.0  # perfect agreement
    assert all(v["deviation"] == 0.0 for v in a["venues"])


def test_mispriced_venue_direction():
    # Deep Kalshi anchors fair value near 0.60; thin Polymarket at 0.72 is the
    # outlier (dev ~+0.10) -> it's the mispriced venue, sell it (buy NO).
    markets = [_m(Venue.KALSHI, 0.60, 900_000), _m(Venue.POLYMARKET, 0.72, 100_000)]
    hit = fairvalue.mispriced_venue(markets, min_deviation=0.03)
    assert hit is not None
    assert hit["venue"] == "polymarket"      # furthest from fair
    assert hit["side"] == "no"               # above fair -> sell (buy NO)
    assert hit["deviation"] > 0


def test_mispriced_venue_none_when_tight():
    markets = [_m(Venue.KALSHI, 0.50, 500), _m(Venue.POLYMARKET, 0.505, 500)]
    assert fairvalue.mispriced_venue(markets, min_deviation=0.03) is None


@pytest.mark.asyncio
async def test_compare_reports_fair_value(client):
    r = (await client.call_tool(
        "compare_across_venues", {"event": "bitcoin above 100k end of 2026"}
    )).data
    assert r["matched"] is True
    assert 0 <= r["fair_value"] <= 1
    assert 0 <= r["fair_value_confidence"] <= 1
    assert len(r["deviations"]) == 2


@pytest.mark.asyncio
async def test_find_mispricing_carries_fair_value(client):
    r = (await client.call_tool("find_mispricing", {"min_edge": 0.0})).data
    cross = [o for o in r["opportunities"] if o["kind"] == "cross_venue"]
    assert cross, "expected a cross-venue opportunity in the mock"
    assert all(o["fair_value"] is None or 0 <= o["fair_value"] <= 1 for o in cross)
    assert any(o["fair_value"] is not None for o in cross)
