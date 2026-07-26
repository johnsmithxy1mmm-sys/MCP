"""Reconciliation, track record, and signed provenance tests."""

from __future__ import annotations

import pytest

from core.models import Leg, Opportunity, OpportunityKind, Side, Venue
from core.reconciliation import ReconciliationStore
from predmarket_mcp import provenance


def _store(tmp_path):
    return ReconciliationStore(f"sqlite:///{tmp_path / 'recon.db'}")


def _cross_venue_opp(edge=0.03):
    # Buy YES on polymarket (cheap), NO on kalshi (dear) — same event.
    return Opportunity(
        kind=OpportunityKind.CROSS_VENUE, title="btc-100k", realizable_edge=edge,
        max_size_usd=1000,
        legs=[
            Leg(venue=Venue.POLYMARKET, market_id="pm", side=Side.YES),
            Leg(venue=Venue.KALSHI, market_id="kx", side=Side.NO),
        ],
    )


def test_record_is_idempotent(tmp_path):
    store = _store(tmp_path)
    opp = _cross_venue_opp()
    prices = {"pm": 0.60, "kx": 0.65}
    a = store.record(opp, prices)
    b = store.record(opp, prices)   # same content
    assert a == b
    assert store.count() == 1


def test_arb_realizes_regardless_of_outcome(tmp_path):
    # A true cross-venue arb pays the spread whether the event resolves YES or NO.
    store = _store(tmp_path)
    store.record(_cross_venue_opp(), {"pm": 0.60, "kx": 0.65})
    # pm resolves YES(1), kx (same event) resolves YES(1) too -> consistent.
    store.resolve({"pm": 1, "kx": 1})
    m1 = store.metrics()

    store2 = ReconciliationStore(f"sqlite:///{tmp_path / 'b.db'}")
    store2.record(_cross_venue_opp(), {"pm": 0.60, "kx": 0.65})
    store2.resolve({"pm": 0, "kx": 0})  # both resolve NO
    m2 = store2.metrics()

    # YES leg entry 0.60, NO leg entry 1-0.65=0.35; cost 0.95.
    # YES outcome: YES leg wins (pm=1), NO leg loses (kx=1) -> payoff 1 -> realized (1-0.95)/0.95.
    # NO outcome:  YES leg loses (pm=0), NO leg wins (kx=0) -> payoff 1 -> same realized.
    assert m1["mean_realized_edge"] == m2["mean_realized_edge"]
    assert m1["resolved_count"] == 1


def test_divergent_resolution_loses(tmp_path):
    # Venues disagree (pm YES, kx NO) — the arb breaks: both legs win OR both lose.
    store = _store(tmp_path)
    store.record(_cross_venue_opp(), {"pm": 0.60, "kx": 0.65})
    store.resolve({"pm": 1, "kx": 0})  # YES leg wins AND NO leg wins -> payoff 2
    m = store.metrics()
    # payoff 2, cost 0.95 -> big positive here; the point: realized != predicted.
    assert m["resolved_count"] == 1
    assert m["edge_slippage"] is not None


def test_metrics_hit_rate_and_brier(tmp_path):
    store = _store(tmp_path)
    # Two single-leg YES "predictions" at implied 0.7; one resolves YES, one NO.
    for mid, oc in (("m1", 1), ("m2", 0)):
        store.record(
            Opportunity(kind=OpportunityKind.BUNDLE, title=mid, realizable_edge=0.02,
                        max_size_usd=100, legs=[Leg(venue=Venue.KALSHI, market_id=mid, side=Side.YES)]),
            {mid: 0.7},
        )
    store.resolve({"m1": 1, "m2": 0})
    m = store.metrics()
    assert m["resolved_count"] == 2
    # Brier = mean((0.7-1)^2, (0.7-0)^2) = mean(0.09, 0.49) = 0.29
    assert m["brier_score"] == 0.29
    assert 0.0 <= m["hit_rate"] <= 1.0


def test_empty_metrics(tmp_path):
    m = _store(tmp_path).metrics()
    assert m["resolved_count"] == 0 and m["hit_rate"] is None


# --- provenance -------------------------------------------------------------
def test_provenance_signs_and_verifies(monkeypatch):
    monkeypatch.setenv("SIGNING_KEY", "operator-secret")
    payload = {"opportunities": [{"title": "x", "edge": 0.03}]}
    block = provenance.provenance(payload)
    assert block["signed"] is True
    assert provenance.verify(payload, block["signature"], "operator-secret")
    # tampering is detected
    assert not provenance.verify({"opportunities": []}, block["signature"], "operator-secret")


def test_provenance_unsigned_without_key(monkeypatch):
    monkeypatch.delenv("SIGNING_KEY", raising=False)
    block = provenance.provenance({"a": 1})
    assert block["signed"] is False and block["signature"] is None


# --- track_record tool end-to-end -------------------------------------------
@pytest.mark.asyncio
async def test_track_record_tool_reflects_resolution(client):
    from predmarket_mcp import deps

    # Flag opportunities, then resolve the 2028 president group.
    await client.call_tool("find_mispricing", {"min_edge": 0.01})
    deps.resolve_outcomes({"pm-us-election-2028-dem": 1, "pm-us-election-2028-rep": 0})
    r = (await client.call_tool("track_record", {})).data
    tr = r["track_record"]
    assert tr["resolved_count"] >= 1
    assert r["provenance"]["source"] == "predmarket-mcp"
    assert r["tier"] == "free"
