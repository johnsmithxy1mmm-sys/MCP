"""Deeper-intelligence tests: calibrated costs, risk adjustment, Dutch book."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core import algorithms
from core.models import (
    Leg,
    Market,
    OrderbookLevel,
    OrderbookSnapshot,
    Opportunity,
    OpportunityKind,
    Side,
    Venue,
)


# --- calibrated fee model ---------------------------------------------------
def test_kalshi_fee_is_price_dependent():
    # Kalshi fee ~ 0.07 * notional * (1 - price): peaks near 50c, ~0 at the tails.
    mid = algorithms.venue_fee(Venue.KALSHI, 1000, 0.5)
    tail = algorithms.venue_fee(Venue.KALSHI, 1000, 0.95)
    assert mid == round(0.07 * 1000 * 0.5, 4)
    assert mid > tail
    assert algorithms.venue_fee(Venue.POLYMARKET, 1000, 0.5) == 0.0


def _book(venue, price):
    return OrderbookSnapshot(
        venue=venue, market_id="m", as_of=datetime.now(timezone.utc),
        yes_asks=[OrderbookLevel(price=price, size_usd=10000)],
        yes_bids=[OrderbookLevel(price=price, size_usd=10000)],
    )


def test_estimate_populates_cost_breakdown():
    books = {("kalshi", "m"): _book(Venue.KALSHI, 0.5)}
    est = algorithms.estimate_realizable_edge(
        [Leg(venue=Venue.KALSHI, market_id="m", side=Side.YES)],
        1000,
        lambda v, mid: books.get((v, mid)),
    )
    assert "kalshi" in est.cost_breakdown
    assert est.cost_breakdown["kalshi"]["fees_usd"] > 0
    assert est.fees_usd > 0


# --- risk adjustment --------------------------------------------------------
def test_annotate_risk_annualizes():
    opp = Opportunity(
        kind=OpportunityKind.CROSS_VENUE, title="x", realizable_edge=0.02,
        max_size_usd=1000, legs=[Leg(venue=Venue.KALSHI, market_id="m", side=Side.YES)],
    )
    close = datetime.now(timezone.utc) + timedelta(days=365)
    algorithms.annotate_risk(opp, close, resolution_risk=0.03)
    assert 360 <= opp.holding_days <= 366
    assert abs(opp.annualized_edge - 0.02) < 0.001   # ~1 year -> ~= raw edge
    assert opp.resolution_risk == 0.03


def test_annotate_risk_shorter_horizon_higher_annualized():
    opp = Opportunity(
        kind=OpportunityKind.BUNDLE, title="x", realizable_edge=0.02,
        max_size_usd=1000, legs=[Leg(venue=Venue.KALSHI, market_id="m", side=Side.YES)],
    )
    algorithms.annotate_risk(opp, datetime.now(timezone.utc) + timedelta(days=30))
    assert opp.annualized_edge > opp.realizable_edge   # 30d edge annualizes up


# --- Dutch book -------------------------------------------------------------
def _grp(mid, yes, group="g"):
    return Market(venue=Venue.POLYMARKET, market_id=mid, title=mid,
                  yes_price=yes, no_price=round(1 - yes, 4), volume_usd=1_000_000,
                  event_group=group, category="politics")


def test_dutch_book_under_round_buys_all_yes():
    # YES sum 0.97 -> buy every YES: net = (1 - 0.97) - haircut(0.01) = 0.02.
    markets = [_grp("dem", 0.52), _grp("rep", 0.45)]
    ops = algorithms.scan_dutch_book(markets, min_edge=0.01)
    assert len(ops) == 1
    o = ops[0]
    assert o.kind == OpportunityKind.DUTCH_BOOK
    assert all(l.side == Side.YES for l in o.legs)
    assert o.realizable_edge == 0.02


def test_dutch_book_over_round_buys_all_no():
    markets = [_grp("a", 0.6), _grp("b", 0.55)]  # sum 1.15 -> buy every NO
    ops = algorithms.scan_dutch_book(markets, min_edge=0.01)
    assert len(ops) == 1
    assert all(l.side == Side.NO for l in ops[0].legs)


def test_dutch_book_ignores_singletons_and_fair_books():
    fair = [_grp("a", 0.5, "fair"), _grp("b", 0.5, "fair")]     # sum 1.0 -> none
    lone = [_grp("c", 0.3, "lone")]                              # single -> none
    assert algorithms.scan_dutch_book(fair + lone, min_edge=0.01) == []


# --- integration through the mock engine ------------------------------------
def test_find_mispricing_surfaces_dutch_book_with_risk():
    from predmarket_mcp import deps

    ops = deps.scan_opportunities(0.01, kind="dutch_book", category=None)
    assert ops, "mock seed has a 2028-president Dutch book"
    o = ops[0]
    assert o.kind.value == "dutch_book"
    assert o.annualized_edge is not None and o.holding_days is not None


# --- honest execution edge (release audit) -----------------------------------
def test_naked_leg_edge_is_cost_drag_not_profit():
    # Regression: a single YES buy used to report ~97% "realizable edge" because
    # every filled dollar was counted as profit. A naked leg has no guaranteed
    # payout — its edge is the (negative) execution drag.
    books = {("kalshi", "m"): _book(Venue.KALSHI, 0.5)}
    est = algorithms.estimate_realizable_edge(
        [Leg(venue=Venue.KALSHI, market_id="m", side=Side.YES)],
        1000, lambda v, mid: books.get((v, mid)))
    assert est.realizable_edge <= 0
    assert est.gross_edge == 0.0


def test_complementary_basket_edge_matches_spread():
    # YES at 0.60 ask + NO at (1 - 0.70 bid) = unit cost 0.90 -> ~11% gross on
    # a guaranteed $1 payout, minus kalshi fees on both legs.
    from core.models import OrderbookSnapshot, OrderbookLevel
    from core.models import utcnow

    def book(price):
        lvl = [OrderbookLevel(price=price, size_usd=100_000)]
        return OrderbookSnapshot(venue=Venue.KALSHI, market_id="x", as_of=utcnow(),
                                 yes_asks=lvl, yes_bids=lvl)
    books = {("kalshi", "a"): book(0.60), ("kalshi", "b"): book(0.70)}
    est = algorithms.estimate_realizable_edge(
        [Leg(venue=Venue.KALSHI, market_id="a", side=Side.YES),
         Leg(venue=Venue.KALSHI, market_id="b", side=Side.NO)],
        1000, lambda v, mid: books.get((v, mid)))
    assert est.gross_edge == pytest.approx(1.0 - (0.60 + 0.30), abs=1e-6)
    assert 0 < est.realizable_edge < est.gross_edge / 0.9  # net of fees, sane scale


def test_all_no_basket_uses_n_minus_1_payoff():
    # Over-round dutch book: N NO legs pay N-1. Sum of YES = 1.10 -> ~10% gross.
    from core.models import OrderbookSnapshot, OrderbookLevel, utcnow

    def book(price):
        lvl = [OrderbookLevel(price=price, size_usd=100_000)]
        return OrderbookSnapshot(venue=Venue.POLYMARKET, market_id="x", as_of=utcnow(),
                                 yes_asks=lvl, yes_bids=lvl)
    books = {("polymarket", "a"): book(0.60), ("polymarket", "b"): book(0.50)}
    est = algorithms.estimate_realizable_edge(
        [Leg(venue=Venue.POLYMARKET, market_id="a", side=Side.NO),
         Leg(venue=Venue.POLYMARKET, market_id="b", side=Side.NO)],
        1000, lambda v, mid: books.get((v, mid)))
    # unit cost = (1-0.6)+(1-0.5)=0.9; payoff (N-1)=1 -> gross 0.10
    assert est.gross_edge == pytest.approx(0.10, abs=1e-6)
    assert est.realizable_edge > 0


def test_basket_with_dead_leg_is_not_executable():
    # Regression: a 2-leg basket whose second book is missing must report 0
    # fillable/edge — selling the surviving leg as a fillable arb is a lie.
    books = {("kalshi", "m"): _book(Venue.KALSHI, 0.5)}
    est = algorithms.estimate_realizable_edge(
        [Leg(venue=Venue.KALSHI, market_id="m", side=Side.YES),
         Leg(venue=Venue.KALSHI, market_id="dead", side=Side.NO)],
        1000, lambda v, mid: books.get((v, mid)))
    assert est.realizable_edge == 0.0 and est.fillable_size_usd == 0.0


# --- cost model (sourced fee schedules, env-overridable) ---------------------
def test_kalshi_fee_matches_published_formula():
    # Published: fee = ceil(rate * C * P * (1-P)); with C = notional/P this is
    # rate * notional * (1-P). At P=0.5 the cost peaks.
    from core.algorithms import venue_fee

    fee = venue_fee(Venue.KALSHI, 1000.0, 0.5)
    assert fee == pytest.approx(0.07 * 1000 * 0.5, abs=0.01)   # ~35
    # Vanishes at the tails, peaks mid-book — not a flat bps.
    assert venue_fee(Venue.KALSHI, 1000.0, 0.99) < venue_fee(Venue.KALSHI, 1000.0, 0.5)


def test_kalshi_fee_rounds_up_to_the_cent():
    # The venue rounds each trade UP; understating a real cost is the one error
    # an "honest realizable edge" product must never make.
    from core.algorithms import venue_fee

    fee = venue_fee(Venue.KALSHI, 1.0, 0.5)   # raw 0.035 -> must ceil to 0.04
    assert fee == pytest.approx(0.04, abs=1e-9)


def test_maker_pays_no_taker_fee():
    from core.algorithms import venue_fee

    assert venue_fee(Venue.KALSHI, 1000.0, 0.5, maker=True) == 0.0
    assert venue_fee(Venue.POLYMARKET, 1000.0, 0.5, maker=True) == 0.0


def test_polymarket_has_no_base_trading_fee():
    from core.algorithms import venue_fee

    assert venue_fee(Venue.POLYMARKET, 1000.0, 0.5) == 0.0


def test_fee_and_gas_are_env_overridable(monkeypatch):
    # Fee schedules change; a stale constant silently corrupts every edge.
    from core.algorithms import gas_for, venue_fee

    monkeypatch.setenv("KALSHI_FEE_RATE", "0.10")
    assert venue_fee(Venue.KALSHI, 1000.0, 0.5) == pytest.approx(50.0, abs=0.01)
    monkeypatch.setenv("POLYMARKET_FEE_RATE", "0.02")
    assert venue_fee(Venue.POLYMARKET, 1000.0, 0.5) == pytest.approx(20.0, abs=0.01)
    monkeypatch.setenv("GAS_USD_POLYMARKET", "0.25")
    assert gas_for(Venue.POLYMARKET) == 0.25


def test_gas_override_reaches_the_edge_estimate(monkeypatch):
    # Regression: gas used to be frozen into module-level dicts at import time
    # (DEFAULT_GAS_USD / mock._GAS_USD), so a runtime GAS_USD_* override never
    # reached the estimate. Gas must resolve per leg AT CALL TIME.
    monkeypatch.setenv("GAS_USD_POLYMARKET", "5.00")
    books = {("polymarket", "m"): _book(Venue.POLYMARKET, 0.5)}
    est = algorithms.estimate_realizable_edge(
        [Leg(venue=Venue.POLYMARKET, market_id="m", side=Side.YES)],
        1000, lambda v, mid: books.get((v, mid)))
    assert est.gas_usd == pytest.approx(5.00, abs=1e-9)
    # ...and the mock engine (which passes no override) picks it up too.
    from core.mock import realizable_edge

    est2 = realizable_edge(
        [Leg(venue=Venue.POLYMARKET, market_id="pm-btc-100k-2026", side=Side.YES)], 1000)
    assert est2.gas_usd == pytest.approx(5.00, abs=1e-9)


def test_explicit_gas_override_dict_still_wins(monkeypatch):
    # Callers that pass an explicit per-venue mapping keep full control.
    monkeypatch.setenv("GAS_USD_POLYMARKET", "5.00")
    books = {("polymarket", "m"): _book(Venue.POLYMARKET, 0.5)}
    est = algorithms.estimate_realizable_edge(
        [Leg(venue=Venue.POLYMARKET, market_id="m", side=Side.YES)],
        1000, lambda v, mid: books.get((v, mid)),
        gas_usd={Venue.POLYMARKET: 0.10})
    assert est.gas_usd == pytest.approx(0.10, abs=1e-9)
