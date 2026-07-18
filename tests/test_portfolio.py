"""F4: portfolio advisor — exposure, correlation, Kelly, hedges."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core.portfolio import _pearson, assess
from core.models import Leg, Market, PricePoint, Side, Venue


def _mkt(mid, title, yes, venue=Venue.POLYMARKET):
    return Market(venue=venue, market_id=mid, title=title, yes_price=yes,
                  no_price=round(1 - yes, 4), category="crypto")


def test_pearson_basics():
    assert _pearson([1, 2, 3, 4], [1, 2, 3, 4]) == 1.0     # perfectly correlated
    assert _pearson([1, 2, 3, 4], [4, 3, 2, 1]) == -1.0    # anti-correlated
    assert _pearson([1], [1]) is None                       # too few points


def _getters(markets, series):
    def market_getter(venue, mid):
        return markets.get(mid)

    def history_getter(venue, mid):
        t0 = datetime(2026, 6, 1, tzinfo=timezone.utc)
        return [PricePoint(ts=t0 + timedelta(minutes=i), yes_price=p)
                for i, p in enumerate(series.get(mid, []))]

    def related_getter(entity):
        return [m for m in markets.values() if entity.split()[0] in (m.title or "").lower()]

    return market_getter, history_getter, related_getter


def test_net_exposure_groups_by_entity():
    markets = {
        "a": _mkt("a", "Bitcoin above $100k at end of 2026", 0.6),
        "b": _mkt("b", "Bitcoin above $150k at end of 2026", 0.4),
    }
    legs = [Leg(venue=Venue.POLYMARKET, market_id="a", side=Side.YES),
            Leg(venue=Venue.POLYMARKET, market_id="b", side=Side.YES)]
    mg, hg, rg = _getters(markets, {})
    out = assess(legs, mg, hg, rg)
    # Both legs are the same entity (bitcoin) -> one concentrated net-long bet.
    assert out["net_exposure"][0]["net"] == 2
    assert out["concentration"]["share"] == 1.0
    assert out["concentration"]["warning"] is None  # single entity, no diversification claim


def test_correlation_detected_from_history():
    markets = {"a": _mkt("a", "Bitcoin above $100k 2026", 0.6),
               "b": _mkt("b", "Bitcoin above $120k 2026", 0.5)}
    series = {"a": [0.50, 0.55, 0.60, 0.65, 0.70], "b": [0.40, 0.45, 0.50, 0.55, 0.60]}
    legs = [Leg(venue=Venue.POLYMARKET, market_id="a", side=Side.YES),
            Leg(venue=Venue.POLYMARKET, market_id="b", side=Side.YES)]
    mg, hg, rg = _getters(markets, series)
    out = assess(legs, mg, hg, rg)
    assert out["correlations"][0]["correlation"] == 1.0  # perfectly co-moving


def test_portfolio_kelly_shrinks_for_correlation():
    markets = {"a": _mkt("a", "Bitcoin above $100k 2026", 0.5),
               "b": _mkt("b", "Bitcoin above $120k 2026", 0.5)}
    series = {"a": [0.4, 0.5, 0.6, 0.7], "b": [0.4, 0.5, 0.6, 0.7]}  # corr = 1
    legs = [Leg(venue=Venue.POLYMARKET, market_id="a", side=Side.YES),
            Leg(venue=Venue.POLYMARKET, market_id="b", side=Side.YES)]
    mg, hg, rg = _getters(markets, series)
    out = assess(legs, mg, hg, rg, bankroll=10_000, fair_values={"a": 0.6, "b": 0.6})
    s = out["sizing"]
    # Each leg has positive Kelly; correlation 1.0 shrinks the combined stake to ~0.
    assert s["naive_fraction"] > 0
    assert s["combined_fraction"] < s["naive_fraction"]
    assert "recommended_size_usd" in s


def test_kelly_skipped_without_fair_values():
    markets = {"a": _mkt("a", "Bitcoin above $100k 2026", 0.5)}
    legs = [Leg(venue=Venue.POLYMARKET, market_id="a", side=Side.YES)]
    mg, hg, rg = _getters(markets, {})
    out = assess(legs, mg, hg, rg)
    assert out["sizing"]["combined_fraction"] is None


def test_hedges_suggest_opposing_markets():
    markets = {
        "a": _mkt("a", "Bitcoin above $100k 2026", 0.6, Venue.POLYMARKET),
        "b": _mkt("b", "Bitcoin above $100k 2026", 0.55, Venue.KALSHI),  # hedge candidate
    }
    legs = [Leg(venue=Venue.POLYMARKET, market_id="a", side=Side.YES)]
    mg, hg, rg = _getters(markets, {})
    out = assess(legs, mg, hg, rg)
    assert out["hedges"]
    cand_ids = {c["market_id"] for c in out["hedges"][0]["candidates"]}
    assert "b" in cand_ids and "a" not in cand_ids  # the held leg isn't its own hedge


def test_no_resolvable_legs():
    legs = [Leg(venue=Venue.POLYMARKET, market_id="ghost", side=Side.YES)]
    out = assess(legs, lambda v, m: None, lambda v, m: [], lambda e: [])
    assert out["error"] == "no_resolvable_legs"


@pytest.mark.asyncio
async def test_assess_portfolio_tool(client):
    r = (await client.call_tool("assess_portfolio", {
        "legs": [{"venue": "polymarket", "market_id": "pm-btc-100k-2026", "side": "yes"}],
    })).data
    assert "portfolio" in r
    assert "net_exposure" in r["portfolio"]
