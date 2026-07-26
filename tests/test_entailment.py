"""C1: logical-entailment + term-structure arbitrage."""

from __future__ import annotations

import pytest

from core.entailment import parse_claim, scan_entailment
from core.models import Market, OpportunityKind, Side, Venue


def _m(title, yes, mid="m", venue=Venue.POLYMARKET, vol=1_000_000):
    return Market(venue=venue, market_id=mid, title=title, yes_price=yes,
                  no_price=round(1 - yes, 4), volume_usd=vol, category="crypto")


# --- parser -----------------------------------------------------------------
def test_parse_threshold_and_direction():
    c = parse_claim(_m("Will Bitcoin close above $150k at end of 2026?", 0.4))
    assert c is not None
    assert c.direction == "above"
    assert c.threshold == 150_000
    assert c.year == 2026
    assert "bitcoin" in c.entity


def test_parse_ignores_year_as_threshold():
    c = parse_claim(_m("Bitcoin above $100,000 on Dec 31 2026", 0.5))
    assert c.threshold == 100_000        # not 2026
    assert c.month == "12"


def test_parse_none_without_threshold():
    assert parse_claim(_m("Will the Democratic nominee win in 2028?", 0.5)) is None


# --- threshold monotonicity -------------------------------------------------
def test_threshold_violation_is_flagged():
    # P(>150k)=0.60 must be <= P(>100k)=0.55 -> violated, risk-free arb.
    markets = [
        _m("Bitcoin above $100k at end of 2026", 0.55, mid="lo"),
        _m("Bitcoin above $150k at end of 2026", 0.60, mid="hi"),
    ]
    opps = scan_entailment(markets, min_edge=0.0)
    assert len(opps) == 1
    o = opps[0]
    assert o.kind == OpportunityKind.ENTAILMENT
    assert o.realizable_edge == pytest.approx(0.60 - 0.55 - 0.01, abs=1e-9)
    # Short the over-priced stronger claim (>150k NO), long the weaker (>100k YES).
    legs = {(l.market_id, l.side) for l in o.legs}
    assert ("hi", Side.NO) in legs and ("lo", Side.YES) in legs


def test_consistent_ladder_has_no_arb():
    markets = [
        _m("Bitcoin above $100k at end of 2026", 0.60, mid="lo"),
        _m("Bitcoin above $150k at end of 2026", 0.40, mid="hi"),  # correctly lower
    ]
    assert scan_entailment(markets, min_edge=0.0) == []


def test_below_direction_monotonicity():
    # For "below", a higher bar must be MORE likely. P(below 100k)=0.30 but
    # P(below 150k)=0.20 is a violation (should be >= 0.30).
    markets = [
        _m("Bitcoin below $100k at end of 2026", 0.30, mid="lo"),
        _m("Bitcoin below $150k at end of 2026", 0.20, mid="hi"),
    ]
    opps = scan_entailment(markets, min_edge=0.0)
    assert len(opps) == 1
    assert opps[0].realizable_edge == pytest.approx(0.30 - 0.20 - 0.01, abs=1e-9)


# --- term structure ---------------------------------------------------------
def test_term_structure_violation():
    # Cumulative "reach by DATE": P(by 2026) must be <= P(by 2027). 0.50 > 0.40 is a violation.
    markets = [
        _m("Will Bitcoin reach $200k by end of 2026?", 0.50, mid="early"),
        _m("Will Bitcoin reach $200k by end of 2027?", 0.40, mid="late"),
    ]
    opps = scan_entailment(markets, min_edge=0.0)
    assert len(opps) == 1
    legs = {(l.market_id, l.side) for l in opps[0].legs}
    assert ("early", Side.NO) in legs and ("late", Side.YES) in legs


def test_point_in_time_not_treated_as_cumulative():
    # "close above ... at end of" is a snapshot, NOT cumulative -> no term-structure arb
    # even though the later date is priced lower (which is legitimate for snapshots).
    markets = [
        _m("Bitcoin close above $200k at end of 2026", 0.50, mid="a"),
        _m("Bitcoin close above $200k at end of 2027", 0.40, mid="b"),
    ]
    assert scan_entailment(markets, min_edge=0.0) == []


# --- integration ------------------------------------------------------------
@pytest.mark.asyncio
async def test_find_mispricing_entailment_kind_filter(client):
    r = (await client.call_tool(
        "find_mispricing", {"min_edge": 0.0, "kind": "entailment"}
    )).data
    assert all(o["kind"] == "entailment" for o in r["opportunities"])


# --- INV-004: враждебный заголовок рынка не роняет разбор (регрессия аудита) --
@pytest.mark.parametrize("title", [
    "Will BTC close above $" + "9" * 5000 + " in 2026?",   # переполнение float -> inf
    "$" + "1," * 3000 + "0",                                # длинная группировка разрядов
    "above 1e400",                                          # явная бесконечность
    "above " + "9" * 4400,                                  # предел int<->str в Python
    "above 100000000000000000000000000000k",                # запредельный порог
])
def test_hostile_title_never_raises(title):
    """Заголовки приходят от площадок; на части из них вопрос рынка задаёт кто
    угодно. До правки такой заголовок валил find_mispricing для ВСЕХ вызывающих
    (OverflowError в _canon_number). Разбор обязан быть тотальным."""
    from core.entailment import parse_claim
    from core.models import Market, Venue

    m = Market(venue=Venue.KALSHI, market_id="m", title=title,
               yes_price=0.5, no_price=0.5)
    claim = parse_claim(m)          # не должно бросать
    if claim is not None:
        import math
        assert math.isfinite(claim.threshold)


def test_hostile_title_does_not_break_the_scan(monkeypatch):
    """Один отравленный рынок не должен ронять весь платный скан."""
    from core import mock
    from core.models import Market, Venue

    poison = Market(venue=Venue.POLYMARKET, market_id="pm-poison",
                    title="Will BTC close above $" + "9" * 5000 + " in 2026?",
                    category="crypto", yes_price=0.5, no_price=0.5, volume_usd=10_000)
    monkeypatch.setattr(mock, "_MARKETS", [*mock._MARKETS, poison])
    opps = mock.scan_opportunities(0.0)      # не должно бросать
    assert isinstance(opps, list)


def test_hostile_title_does_not_break_the_matcher():
    """Тот же вход проходит и через матчер (_normalize -> _canon_number)."""
    from core.algorithms import title_similarity

    evil = "above $" + "9" * 5000
    assert 0.0 <= title_similarity(evil, "Bitcoin above $100k") <= 1.0
