"""K2: scenario engine — propagate assumptions across the market graph."""

from __future__ import annotations

import json

import pytest

from core.models import Leg, Market, Side, Venue
from core.scenario import portfolio_impact, propagate


def _m(mid, yes):
    return Market(venue=Venue.POLYMARKET, market_id=mid, title=mid,
                  yes_price=yes, no_price=round(1 - yes, 4), volume_usd=1_000_000)


def _no_history(venue, market_id):
    return []


# --- propagation ------------------------------------------------------------
def test_entailment_pin_yes_drives_weaker_claim_up():
    markets = [_m("btc-100k", 0.71), _m("btc-150k", 0.45)]
    logical = {("btc-150k", "btc-100k"): 1.0}  # 150k=yes => 100k=yes (P=1)
    shifts = propagate({"btc-150k": 1.0}, markets, _no_history, logical)
    row = next(s for s in shifts if s["market_id"] == "btc-100k")
    assert row["posterior"] == 1.0 and row["shift"] == pytest.approx(0.29, abs=1e-4)
    assert row["drivers"][0]["source"] == "logical"


def test_mutual_exclusion_pin_yes_drives_peer_to_zero():
    markets = [_m("dem", 0.52), _m("rep", 0.45)]
    logical = {("dem", "rep"): 0.0}  # dem=yes => rep=no (P=0)
    shifts = propagate({"dem": 1.0}, markets, _no_history, logical)
    row = next(s for s in shifts if s["market_id"] == "rep")
    assert row["posterior"] == 0.0


def test_pin_no_uses_contrapositive_of_entailment():
    # 150k => 100k. If we pin 100k=NO, then 150k must be NO too (¬weak => ¬strong).
    markets = [_m("btc-100k", 0.71), _m("btc-150k", 0.45)]
    logical = {("btc-150k", "btc-100k"): 1.0}  # P(100k | 150k=yes) = 1
    shifts = propagate({"btc-100k": 0.0}, markets, _no_history, logical)
    row = next(s for s in shifts if s["market_id"] == "btc-150k")
    assert row["posterior"] == 0.0
    assert row["drivers"][0]["source"] == "logical"


def test_pinned_markets_are_not_in_shifts():
    markets = [_m("a", 0.5), _m("b", 0.5)]
    shifts = propagate({"a": 1.0}, markets, _no_history, {})
    assert all(s["market_id"] != "a" for s in shifts)


# --- portfolio impact -------------------------------------------------------
def test_portfolio_impact_signs_by_side():
    shifts = [{"market_id": "x", "shift": 0.20}, {"market_id": "y", "shift": -0.10}]
    legs = [Leg(venue=Venue.POLYMARKET, market_id="x", side=Side.YES),   # gains +0.20
            Leg(venue=Venue.POLYMARKET, market_id="y", side=Side.NO)]    # gains +0.10
    out = portfolio_impact(shifts, legs)
    assert out["net_value_impact"] == pytest.approx(0.30, abs=1e-9)
    assert out["affected_legs"] == 2


def test_portfolio_impact_ignores_unaffected_legs():
    shifts = [{"market_id": "x", "shift": 0.20}]
    legs = [Leg(venue=Venue.POLYMARKET, market_id="z", side=Side.YES)]
    out = portfolio_impact(shifts, legs)
    assert out["net_value_impact"] == 0.0 and out["affected_legs"] == 0


# --- through deps + MCP surface ---------------------------------------------
def test_simulate_scenario_is_cluster_scoped():
    from predmarket_mcp import deps

    # A BTC pin must move only BTC markets — not Fed/election (no stopword bleed).
    out = deps.simulate_scenario("pm-btc-150k-2026:yes")
    assert out["found"] is True
    ids = {s["market_id"] for s in out["shifts"]}
    assert "pm-btc-100k-2026" in ids
    assert not any("fed" in i or "election" in i for i in ids)


def test_simulate_scenario_unknown_pin():
    from predmarket_mcp import deps

    out = deps.simulate_scenario("does-not-exist:yes")
    assert out["found"] is False and "does-not-exist" in out["unknown_markets"]


@pytest.mark.asyncio
async def test_scenario_resource_shape(client):
    res = await client.read_resource("scenario://scan/pm-btc-150k-2026:yes")
    data = json.loads(res[0].text)
    assert data["found"] is True
    assert data["assumptions"][0]["market_id"] == "pm-btc-150k-2026"
    row = next(s for s in data["shifts"] if s["market_id"] == "pm-btc-100k-2026")
    assert row["shift"] > 0  # 150k=yes entails 100k=yes


@pytest.mark.asyncio
async def test_assess_portfolio_scenario_stress(client):
    legs = [{"venue": "polymarket", "market_id": "pm-btc-100k-2026", "side": "yes"}]
    r = (await client.call_tool(
        "assess_portfolio", {"legs": legs, "scenario": "pm-btc-150k-2026:yes"})).data
    assert "scenario_impact" in r
    impact = r["scenario_impact"]
    assert impact["net_value_impact"] > 0  # 100k YES gains when 150k is assumed YES
