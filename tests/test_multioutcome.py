"""G: native multi-outcome markets — de-vig + completeness-aware dutch book."""

from __future__ import annotations

import json

import pytest

from core.multioutcome import build_from_markets, full_dutch_book, view
from core.models import Market, Side, Venue


def _m(mid, yes, group="race", venue=Venue.POLYMARKET, vol=1_000_000):
    return Market(venue=venue, market_id=mid, title=mid, yes_price=yes,
                  no_price=round(1 - yes, 4), category="politics",
                  volume_usd=vol, event_group=group)


# --- build + de-vig ---------------------------------------------------------
def test_build_groups_by_event_group():
    markets = [_m("a", 0.5), _m("b", 0.4), _m("solo", 0.3, group="other")]
    moms = build_from_markets(markets)
    race = [m for m in moms if m.event == "race"][0]
    assert len(race.outcomes) == 2
    # 'other' has a single member -> not a multi-outcome market.
    assert all(m.event != "other" for m in moms)


def test_overround_and_devig():
    # Three outcomes summing to 1.20 -> 20% overround; de-vig to sum 1.
    markets = [_m("a", 0.5), _m("b", 0.4), _m("c", 0.3)]
    mom = build_from_markets(markets)[0]
    assert mom.total_probability == 1.2
    assert mom.overround == 0.2
    fair = mom.normalized
    assert abs(sum(o["fair_probability"] for o in fair) - 1.0) < 1e-6
    assert mom.favorite == "a"


# --- completeness-aware dutch book ------------------------------------------
def test_overround_is_always_arbitrage():
    # Σp = 1.20 > 1 -> sell the book (buy NO on every outcome), no completeness needed.
    mom = build_from_markets([_m("a", 0.5), _m("b", 0.4), _m("c", 0.3)])[0]
    arb = full_dutch_book(mom, min_edge=0.0)
    assert arb is not None
    assert all(l.side == Side.NO for l in arb.legs)
    assert arb.realizable_edge == pytest.approx(0.20 - 0.01, abs=1e-9)


def test_underround_arbitrage_requires_completeness():
    # Σp = 0.90 < 1. Only free money if the set covers every outcome.
    incomplete = build_from_markets([_m("a", 0.5), _m("b", 0.4)])[0]
    assert full_dutch_book(incomplete, min_edge=0.0) is None  # incomplete -> no false arb

    complete = build_from_markets([_m("a", 0.5), _m("b", 0.4)],
                                  complete_groups={"race"})[0]
    arb = full_dutch_book(complete, min_edge=0.0)
    assert arb is not None
    assert all(l.side == Side.YES for l in arb.legs)  # buy YES on every outcome


def test_view_shape():
    mom = build_from_markets([_m("a", 0.5), _m("b", 0.4), _m("c", 0.35)])[0]
    v = view(mom)
    assert v["outcomes"] == 3
    assert v["overround"] == round(1.25 - 1.0, 4)
    assert v["fair_probabilities"] and v["favorite"] == "a"
    assert v["dutch_book"] is not None  # over-round


# --- adapters populate event_group ------------------------------------------
def test_polymarket_populates_event_group(monkeypatch):
    import httpx
    from core.adapters import base, polymarket

    rows = [{"conditionId": "0x1", "question": "Candidate A wins?",
             "outcomes": "[\"Yes\",\"No\"]", "outcomePrices": "[\"0.5\",\"0.5\"]",
             "negRiskMarketID": "race-42", "volume": "1000"}]

    def make_client(base_url, timeout=None):
        return httpx.Client(base_url=base_url,
                            transport=httpx.MockTransport(lambda r: httpx.Response(200, json=rows)))

    monkeypatch.setattr(base, "make_client", make_client)
    base._CLIENTS.clear()
    m = polymarket.PolymarketAdapter().fetch_markets()[0]
    assert m.event_group == "race-42"


# --- resource ---------------------------------------------------------------
@pytest.mark.asyncio
async def test_outcomes_resource(client):
    r = await client.read_resource("outcomes://2028")
    data = json.loads(r[0].text)
    assert data["found"] is True
    mom = data["multi_outcome_markets"][0]
    assert "fair_probabilities" in mom and "overround" in mom
    # The mock's dem+rep sums to 0.97 (< 1) and is NOT marked complete -> no false arb.
    assert mom["dutch_book"] is None


def test_devig_sums_to_one_for_tiny_probabilities():
    """INV-011: де-виг делил на ОКРУГЛЁННУЮ сумму, из-за чего доли уходили от 1
    на ~0.2% при малых вероятностях — рушилось единственное свойство,
    определяющее де-виггирование."""
    from core.models import MultiOutcomeMarket, Outcome, Venue

    for ps in ([0.015625, 0.015625], [0.001, 0.002, 0.003], [0.33, 0.33, 0.33]):
        m = MultiOutcomeMarket(event="e", outcomes=[
            Outcome(name=f"o{i}", venue=Venue.KALSHI, market_id=f"m{i}", yes_price=p)
            for i, p in enumerate(ps)])
        total = sum(o["fair_probability"] for o in m.normalized)
        assert abs(total - 1.0) < 1e-3, f"{ps} -> {total}"
