"""J1: adverse-selection / trap score."""

from __future__ import annotations

import pytest

from core.adverse import assess
from core.models import Leg, Opportunity, OpportunityKind, Side, Venue


def _opp(edge=0.05, side=Side.YES, kind=OpportunityKind.CROSS_VENUE):
    return Opportunity(kind=kind, title="t", category="crypto", realizable_edge=edge,
                       max_size_usd=100.0,
                       legs=[Leg(venue=Venue.KALSHI, market_id="m", side=side)])


def test_clean_opportunity_has_low_trap():
    out = assess(_opp(), momentum=0.1, informed_flow=False,
                 historical_hit=0.7, historical_samples=50, quality_flags=[])
    assert out["trap_score"] == 0.0
    assert out["trust_adjusted_edge"] == 0.05


def test_informed_flow_against_a_long_raises_trap():
    # Long YES while the tape is being walked DOWN (momentum < 0) = adverse.
    out = assess(_opp(side=Side.YES), momentum=-1.0, informed_flow=True,
                 historical_hit=0.7, historical_samples=50)
    assert out["trap_score"] > 0
    assert any("informed flow" in f for f in out["factors"])
    assert out["trust_adjusted_edge"] < 0.05


def test_informed_flow_with_the_position_is_not_adverse():
    # Long YES while the tape moves UP = you're on the right side, no penalty.
    out = assess(_opp(side=Side.YES), momentum=1.0, informed_flow=True,
                 historical_hit=0.7, historical_samples=50)
    assert out["trap_score"] == 0.0


def test_poor_historical_hit_rate_raises_trap():
    out = assess(_opp(), historical_hit=0.2, historical_samples=40)
    assert out["trap_score"] == pytest.approx(0.3, abs=1e-6)  # 0.5 - 0.2
    assert any("historically hit" in f for f in out["factors"])


def test_thin_history_is_ignored(monkeypatch):
    monkeypatch.setenv("ADVERSE_MIN_SAMPLES", "10")
    out = assess(_opp(), historical_hit=0.1, historical_samples=3)  # too few
    assert out["trap_score"] == 0.0  # never penalize on thin evidence


def test_quality_flags_raise_trap():
    out = assess(_opp(), quality_flags=["crossed_book", "stale"])
    assert out["trap_score"] > 0
    assert any("quote quality" in f for f in out["factors"])


def test_trap_is_capped_and_edge_floored():
    out = assess(_opp(edge=0.05, side=Side.YES), momentum=-1.0, informed_flow=True,
                 historical_hit=0.0, historical_samples=50,
                 quality_flags=["crossed_book", "stale", "wide_spread"])
    assert 0.0 <= out["trap_score"] <= 1.0
    assert out["trust_adjusted_edge"] >= 0.0


@pytest.mark.asyncio
async def test_find_mispricing_carries_adverse(client):
    r = (await client.call_tool("find_mispricing", {"min_edge": 0.0})).data
    for o in r["opportunities"]:
        assert "adverse" in o and o["adverse"] is not None
        assert 0.0 <= o["adverse"]["trap_score"] <= 1.0
        assert "trust_adjusted_edge" in o["adverse"]
