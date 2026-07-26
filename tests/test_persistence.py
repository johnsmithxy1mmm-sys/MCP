"""F2: edge-survival model + expected-value ranking."""

from __future__ import annotations

import pytest

from core.models import Leg, Opportunity, OpportunityKind, Side, Venue
from core.persistence import PersistenceStore


def _opp(title, edge=0.05, kind=OpportunityKind.BUNDLE, category="crypto"):
    return Opportunity(kind=kind, title=title, category=category, realizable_edge=edge,
                       max_size_usd=100.0,
                       legs=[Leg(venue=Venue.KALSHI, market_id=title, side=Side.YES)])


def test_lifespan_recorded_when_opportunity_disappears(tmp_path):
    store = PersistenceStore(db_url=f"sqlite:///{tmp_path / 'p.db'}")
    a, b = _opp("A"), _opp("B")
    store.observe([a, b])       # both first seen
    store.observe([a])          # B disappeared -> its lifespan is closed out
    with store._connect() as conn:
        rows = conn.execute("SELECT bucket FROM lifespans").fetchall()
        remaining = conn.execute("SELECT sig FROM sightings").fetchall()
    assert len(rows) == 1                      # B's lifespan recorded
    assert len(remaining) == 1                 # A still tracked


def test_survival_none_until_min_samples(monkeypatch, tmp_path):
    monkeypatch.setenv("PERSISTENCE_MIN_SAMPLES", "5")
    store = PersistenceStore(db_url=f"sqlite:///{tmp_path / 'p.db'}")
    # Seed 2 short lifespans directly — below the threshold.
    with store._connect() as conn:
        for _ in range(2):
            conn.execute("INSERT INTO lifespans (bucket, seconds) VALUES ('bundle|crypto', 10)")
    assert store.survival("bundle|crypto", horizon_seconds=60) is None


def test_survival_reflects_history(monkeypatch, tmp_path):
    monkeypatch.setenv("PERSISTENCE_MIN_SAMPLES", "4")
    store = PersistenceStore(db_url=f"sqlite:///{tmp_path / 'p.db'}")
    with store._connect() as conn:
        # 8 of 10 lifespans exceed the 60s horizon -> high survival.
        for s in (120, 90, 300, 61, 200, 75, 400, 65, 10, 5):
            conn.execute("INSERT INTO lifespans (bucket, seconds) VALUES ('bundle|crypto', ?)", (s,))
    sp = store.survival("bundle|crypto", horizon_seconds=60)
    assert 0.6 < sp < 0.9              # empirical ~8/10, Laplace-smoothed
    assert store.median_lifespan("bundle|crypto") is not None


def test_annotate_ranks_by_expected_value(monkeypatch, tmp_path):
    monkeypatch.setenv("PERSISTENCE_MIN_SAMPLES", "2")
    store = PersistenceStore(db_url=f"sqlite:///{tmp_path / 'p.db'}")
    with store._connect() as conn:
        # 'bundle' opportunities are short-lived (die before the horizon)...
        for s in (5, 8, 3, 6):
            conn.execute("INSERT INTO lifespans (bucket, seconds) VALUES ('bundle|crypto', ?)", (s,))
        # ...'cross_venue' ones last well past it.
        for s in (300, 250, 400, 500):
            conn.execute("INSERT INTO lifespans (bucket, seconds) VALUES ('cross_venue|crypto', ?)", (s,))

    big_but_fleeting = _opp("fleeting", edge=0.08, kind=OpportunityKind.BUNDLE)
    small_but_durable = _opp("durable", edge=0.04, kind=OpportunityKind.CROSS_VENUE)
    ranked = store.annotate([big_but_fleeting, small_but_durable], horizon_seconds=60)
    # Despite a smaller raw edge, the durable opportunity wins on expected value.
    assert ranked[0].title == "durable"
    assert ranked[0].expected_value > ranked[1].expected_value
    assert big_but_fleeting.survival_probability < small_but_durable.survival_probability


def test_annotate_falls_back_to_edge_without_history(tmp_path):
    store = PersistenceStore(db_url=f"sqlite:///{tmp_path / 'p.db'}")
    o = _opp("x", edge=0.05)
    store.annotate([o])
    assert o.survival_probability is None
    assert o.expected_value == 0.05  # no penalty on thin evidence


@pytest.mark.asyncio
async def test_find_mispricing_carries_expected_value(client):
    r = (await client.call_tool("find_mispricing", {"min_edge": 0.0})).data
    for o in r["opportunities"]:
        assert "expected_value" in o and "survival_probability" in o
        assert o["expected_value"] is not None
