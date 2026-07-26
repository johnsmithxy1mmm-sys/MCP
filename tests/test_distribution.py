"""H1: implied distribution engine — survival curve, percentiles, strike arb."""

from __future__ import annotations

import json

import pytest

from core.distribution import (
    build_distribution,
    prob_above,
    strike_arbitrage,
)
from core.models import Market, Venue


def _ladder():
    # P(>100k)=0.70, P(>150k)=0.45, P(>200k)=0.20 — a clean monotone ladder.
    specs = [("100k", 100_000, 0.70), ("150k", 150_000, 0.45), ("200k", 200_000, 0.20)]
    return [Market(venue=Venue.POLYMARKET, market_id=f"btc-{n}",
                   title=f"Will Bitcoin close above ${n} at end of 2026?",
                   yes_price=p, no_price=round(1 - p, 4), category="crypto",
                   volume_usd=1_000_000) for n, _, p in specs]


def test_build_distribution_shape():
    dist = build_distribution(_ladder())
    assert dist["thresholds"] == 3
    curve = dist["survival_curve"]
    assert [pt["prob_above"] for pt in curve] == [0.70, 0.45, 0.20]  # decreasing
    assert dist["percentiles"]["p50_median"] is not None
    assert dist["implied_mean"] > 100_000


def test_median_between_thresholds():
    # Survival crosses 0.5 between 100k (0.70) and 150k (0.45).
    dist = build_distribution(_ladder())
    median = dist["percentiles"]["p50_median"]
    assert 100_000 < median < 150_000


def test_prob_above_interpolates_unlisted_strike():
    # 125k isn't a listed market; the curve implies it between 0.70 and 0.45.
    p = prob_above(_ladder(), 125_000)
    assert 0.45 < p < 0.70
    assert p == pytest.approx(0.575, abs=0.01)  # midpoint of the 100k-150k segment


def test_monotonicity_enforced_on_noisy_ladder():
    # A noisy quote where 150k prints ABOVE 100k is clamped (survival can't rise).
    specs = [("100k", 0.60), ("150k", 0.65), ("200k", 0.20)]
    markets = [Market(venue=Venue.POLYMARKET, market_id=n,
                      title=f"Bitcoin above ${n} at end of 2026", yes_price=p,
                      no_price=round(1 - p, 4), category="crypto") for n, p in specs]
    curve = build_distribution(markets)["survival_curve"]
    probs = [pt["prob_above"] for pt in curve]
    assert probs == sorted(probs, reverse=True)  # non-increasing after clamp


def test_strike_arbitrage_against_curve():
    # A listed 125k market at 0.72 is rich vs the ~0.575 the ladder implies.
    hit = strike_arbitrage(_ladder(), strike=125_000, market_price=0.72)
    assert hit is not None
    assert hit["curve_implied"] == pytest.approx(0.575, abs=0.01)
    assert hit["side"] == "no"        # sell the over-priced strike
    assert hit["edge"] > 0


def test_no_arb_when_strike_matches_curve():
    assert strike_arbitrage(_ladder(), 125_000, market_price=0.575) is None


def test_none_on_single_threshold():
    one = [Market(venue=Venue.POLYMARKET, market_id="x",
                  title="Bitcoin above $100k at end of 2026", yes_price=0.5, no_price=0.5)]
    assert build_distribution(one) is None


@pytest.mark.asyncio
async def test_distribution_resource(client):
    r = await client.read_resource("distribution://bitcoin")
    data = json.loads(r[0].text)
    assert data["found"] is True
    assert data["thresholds"] >= 2
    assert "percentiles" in data and "survival_curve" in data
