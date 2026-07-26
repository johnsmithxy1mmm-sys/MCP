"""J4: calibration-weighted meta-consensus."""

from __future__ import annotations

import pytest

from core.metaconsensus import VenueAccuracyStore, meta_consensus
from core.models import Market, Venue


def _m(venue, yes):
    return Market(venue=venue, market_id=f"{venue.value}-x", title="btc",
                  yes_price=yes, no_price=round(1 - yes, 4), volume_usd=100_000)


# --- accuracy store ---------------------------------------------------------
def test_accuracy_neutral_until_min_samples(monkeypatch, tmp_path):
    monkeypatch.setenv("ACCURACY_MIN_SAMPLES", "10")
    store = VenueAccuracyStore(db_url=f"sqlite:///{tmp_path / 'a.db'}")
    store.record("kalshi", 0.6, 1)
    acc = store.accuracy("kalshi")
    assert acc["calibrated"] is False and acc["weight"] == 0.5


def test_accuracy_rewards_low_brier(monkeypatch, tmp_path):
    monkeypatch.setenv("ACCURACY_MIN_SAMPLES", "4")
    store = VenueAccuracyStore(db_url=f"sqlite:///{tmp_path / 'a.db'}")
    # Kalshi is well-calibrated (0.9 -> YES, 0.1 -> NO); Polymarket is badly off.
    for _ in range(5):
        store.record("kalshi", 0.9, 1)
        store.record("kalshi", 0.1, 0)
        store.record("polymarket", 0.9, 0)   # said 0.9 but resolved NO
        store.record("polymarket", 0.1, 1)   # said 0.1 but resolved YES
    kx = store.accuracy("kalshi")
    pm = store.accuracy("polymarket")
    assert kx["weight"] > pm["weight"]
    assert kx["brier"] < pm["brier"]


# --- consensus fusion -------------------------------------------------------
def test_equal_weight_when_uncalibrated():
    # No history -> neutral weights -> plain average.
    out = meta_consensus([_m(Venue.KALSHI, 0.60), _m(Venue.POLYMARKET, 0.70)],
                         accuracy_fn=lambda v: {"weight": 0.5, "samples": 0, "calibrated": False})
    assert out["estimate"] == pytest.approx(0.65, abs=1e-6)
    assert out["credible_interval"][0] <= out["estimate"] <= out["credible_interval"][1]


def test_accurate_venue_pulls_the_estimate():
    # Kalshi is trusted (weight 0.9), Polymarket barely (0.1) -> estimate near Kalshi.
    weights = {"kalshi": 0.9, "polymarket": 0.1}
    out = meta_consensus([_m(Venue.KALSHI, 0.60), _m(Venue.POLYMARKET, 0.90)],
                         accuracy_fn=lambda v: {"weight": weights[v], "calibrated": True})
    assert out["estimate"] == pytest.approx(0.63, abs=0.01)  # 0.6*0.9+0.9*0.1


def test_zero_weights_fall_back_to_equal():
    out = meta_consensus([_m(Venue.KALSHI, 0.4), _m(Venue.POLYMARKET, 0.6)],
                         accuracy_fn=lambda v: {"weight": 0.0})
    assert out["estimate"] == pytest.approx(0.5, abs=1e-6)


# --- resolution feeds venue accuracy ----------------------------------------
def test_resolution_records_venue_accuracy(tmp_path, monkeypatch):
    from core.reconciliation import ReconciliationStore
    from core.models import Leg, Opportunity, OpportunityKind, Side
    import core.metaconsensus as mc

    acc = VenueAccuracyStore(db_url=f"sqlite:///{tmp_path / 'acc.db'}")
    monkeypatch.setattr(mc, "get_venue_accuracy", lambda: acc)

    recon = ReconciliationStore(db_url=f"sqlite:///{tmp_path / 'r.db'}")
    opp = Opportunity(kind=OpportunityKind.CROSS_VENUE, title="t", category="crypto",
                      realizable_edge=0.05, max_size_usd=100.0,
                      legs=[Leg(venue=Venue.KALSHI, market_id="m1", side=Side.YES)])
    recon.record(opp, {"m1": 0.7})
    recon.resolve({"m1": 1})  # settled YES -> a calibration sample for kalshi
    with acc._connect() as conn:
        rows = conn.execute("SELECT venue, price, outcome FROM venue_samples").fetchall()
    assert len(rows) == 1
    assert rows[0]["venue"] == "kalshi" and rows[0]["outcome"] == 1


@pytest.mark.asyncio
async def test_compare_reports_meta_consensus(client):
    r = (await client.call_tool(
        "compare_across_venues", {"event": "bitcoin above 100k end of 2026"}
    )).data
    assert "meta_consensus" in r
    mc = r["meta_consensus"]
    assert 0 <= mc["estimate"] <= 1
    assert len(mc["credible_interval"]) == 2
