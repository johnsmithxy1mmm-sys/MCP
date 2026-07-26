"""F3: event graph — clustering, relations, transitive entailment."""

from __future__ import annotations

import json

import pytest

from core.eventgraph import build_clusters, event_view, transitive_entailment_violations
from core.models import Market, Venue


def _m(title, yes, mid, venue=Venue.POLYMARKET):
    return Market(venue=venue, market_id=mid, title=title, yes_price=yes,
                  no_price=round(1 - yes, 4), category="crypto")


def test_clusters_group_by_entity_and_relate_thresholds():
    markets = [
        _m("Bitcoin above $100k at end of 2026", 0.55, "lo"),
        _m("Bitcoin above $150k at end of 2026", 0.40, "hi"),
        _m("Will the Democratic nominee win in 2028?", 0.5, "pol"),  # different entity
    ]
    clusters = build_clusters(markets)
    btc = [c for k, c in clusters.items() if "bitcoin" in k][0]
    assert len(btc.markets) == 2
    implies = [r for r in btc.relations if r["type"] == "implies"]
    assert implies and implies[0]["from"] == "hi" and implies[0]["to"] == "lo"


def test_transitive_violation_across_middle_rung():
    # Adjacent steps each look fine (<=1c gap), but 100k->150k across the middle
    # rung is a real violation: P(>150k)=0.60 must be <= P(>100k)=0.55.
    markets = [
        _m("Bitcoin above $100k at end of 2026", 0.55, "a"),
        _m("Bitcoin above $125k at end of 2026", 0.555, "b"),
        _m("Bitcoin above $150k at end of 2026", 0.60, "c"),
    ]
    viols = transitive_entailment_violations(markets, min_edge=0.0)
    pairs = {(v["sell"], v["buy"]) for v in viols}
    assert ("c", "a") in pairs                       # the non-adjacent violation
    assert any(not v["adjacent"] for v in viols)     # transitive one is flagged


def test_consistent_ladder_has_no_violations():
    markets = [
        _m("Bitcoin above $100k at end of 2026", 0.60, "a"),
        _m("Bitcoin above $150k at end of 2026", 0.40, "b"),
    ]
    assert transitive_entailment_violations(markets, min_edge=0.0) == []


def test_same_event_edge_across_venues():
    markets = [
        _m("Bitcoin above $100k at end of 2026", 0.6, "pm", Venue.POLYMARKET),
        _m("Bitcoin above $100k at end of 2026", 0.55, "kx", Venue.KALSHI),
    ]
    clusters = build_clusters(markets)
    rels = next(iter(clusters.values())).relations
    assert any(r["type"] == "same_event" for r in rels)


def test_event_view_shape():
    markets = [
        _m("Bitcoin above $100k at end of 2026", 0.55, "lo"),
        _m("Bitcoin above $150k at end of 2026", 0.60, "hi"),
    ]
    view = event_view("bitcoin", markets)
    assert view["markets"] == 2
    assert view["clusters"] and view["clusters"][0]["relations"]
    assert view["violations"]  # the ladder is inconsistent


@pytest.mark.asyncio
async def test_event_resource(client):
    r = await client.read_resource("event://bitcoin")
    data = json.loads(r[0].text)
    assert "clusters" in data and "markets" in data
