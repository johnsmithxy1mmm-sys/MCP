"""I1: capital-velocity strategy — turnover metrics, ranking, rotation plan."""

from __future__ import annotations

import pytest

from core.velocity import annotate_velocity, rank_by_velocity, rotation_plan
from core.models import Leg, Opportunity, OpportunityKind, Side, Venue


def _opp(title, edge, holding_days=None, max_size=1000.0):
    o = Opportunity(kind=OpportunityKind.CROSS_VENUE, title=title, category="crypto",
                    realizable_edge=edge, max_size_usd=max_size,
                    legs=[Leg(venue=Venue.KALSHI, market_id=title, side=Side.YES)])
    o.holding_days = holding_days
    o.expected_value = edge  # survival unknown -> EV = edge
    return o


# --- metrics ----------------------------------------------------------------
def test_capital_efficiency_prices_time():
    fast = annotate_velocity([_opp("fast", 0.01, holding_days=7)])[0]
    slow = annotate_velocity([_opp("slow", 0.05, holding_days=365)])[0]
    # 1% weekly beats 5% yearly per locked-capital day...
    assert fast.velocity["capital_efficiency"] > slow.velocity["capital_efficiency"]
    # ...and compounds far harder over a year.
    assert fast.velocity["compound_annual_growth"] > slow.velocity["compound_annual_growth"]
    assert slow.velocity["compound_annual_growth"] == pytest.approx(0.05, abs=0.001)


def test_unknown_holding_uses_default_and_is_flagged(monkeypatch):
    monkeypatch.setenv("VELOCITY_DEFAULT_DAYS", "30")
    o = annotate_velocity([_opp("x", 0.02, holding_days=None)])[0]
    assert o.velocity["holding_known"] is False
    assert o.velocity["holding_days"] == 30.0


def test_growth_is_capped():
    o = annotate_velocity([_opp("crazy", 0.10, holding_days=0.5)])[0]
    assert o.velocity["compound_annual_growth"] == 100.0  # capped, not astronomic


# --- ranking ----------------------------------------------------------------
def test_velocity_rank_prefers_fast_recycling():
    slow_big = _opp("slow_big", 0.06, holding_days=300)
    fast_small = _opp("fast_small", 0.015, holding_days=10)
    opps = annotate_velocity([slow_big, fast_small])
    ranked = rank_by_velocity(opps)
    # 0.015/10 > 0.06/300 -> the fast recycler wins under the turnover strategy.
    assert ranked[0].title == "fast_small"


# --- rotation plan ----------------------------------------------------------
def test_rotation_plan_allocates_and_projects():
    fast = _opp("fast", 0.02, holding_days=10, max_size=600)
    slow = _opp("slow", 0.05, holding_days=90, max_size=600)
    too_long = _opp("too_long", 0.08, holding_days=400, max_size=600)
    opps = annotate_velocity([fast, slow, too_long])
    plan = rotation_plan(opps, bankroll_usd=1000, horizon_days=90)

    titles = [a["title"] for a in plan["allocations"]]
    assert "too_long" not in titles                 # beyond the horizon
    assert plan["deployed_usd"] <= 1000
    assert plan["deployed_usd"] + plan["idle_usd"] == pytest.approx(1000, abs=0.01)
    fast_alloc = next(a for a in plan["allocations"] if a["title"] == "fast")
    assert fast_alloc["cycles_in_horizon"] == 9     # 90 // 10 -> capital recycles
    assert plan["projected_bankroll_usd"] > 1000    # positive-EV plan compounds
    assert "not a promise" in plan["note"]


def test_rotation_plan_respects_max_size_and_positions(monkeypatch):
    monkeypatch.setenv("ROTATION_MAX_POSITIONS", "1")
    opps = annotate_velocity([_opp("a", 0.02, 10, max_size=100),
                              _opp("b", 0.02, 10, max_size=100)])
    plan = rotation_plan(opps, bankroll_usd=1000, horizon_days=30)
    assert plan["positions"] == 1
    assert plan["allocations"][0]["allocated_usd"] == 100  # capped by max_size
    assert plan["idle_usd"] == 900


def test_rotation_plan_empty_without_candidates():
    plan = rotation_plan([], bankroll_usd=500, horizon_days=30)
    assert plan["positions"] == 0 and plan["projected_bankroll_usd"] == 500


# --- tool integration -------------------------------------------------------
@pytest.mark.asyncio
async def test_find_mispricing_velocity_rank_and_plan(client):
    r = (await client.call_tool("find_mispricing", {
        "min_edge": 0.0, "rank": "velocity",
        "bankroll_usd": 1000, "horizon_days": 365,
    })).data
    assert r["rank"] == "velocity"
    assert all("velocity" in o and o["velocity"] for o in r["opportunities"])
    scores = [o["velocity"]["velocity_score"] for o in r["opportunities"]]
    assert scores == sorted(scores, reverse=True)   # actually ranked by velocity
    plan = r["rotation_plan"]
    assert plan["positions"] >= 1
    assert plan["projected_bankroll_usd"] >= plan["idle_usd"]


@pytest.mark.asyncio
async def test_default_rank_unchanged(client):
    r = (await client.call_tool("find_mispricing", {"min_edge": 0.0})).data
    assert r["rank"] == "expected_value"
    assert "rotation_plan" not in r  # no bankroll -> no plan
