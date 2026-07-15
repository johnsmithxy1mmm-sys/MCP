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
