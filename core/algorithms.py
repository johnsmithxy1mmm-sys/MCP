"""Venue-agnostic intelligence algorithms.

These operate purely on the canonical models plus a callable to fetch orderbooks,
so both the mock engine and the live (adapter-backed) engine reuse the same math
instead of duplicating it:

* ``estimate_realizable_edge`` — the edge calculation (walk depth, apply fees +
  gas + slippage). Identical for mock and live; both delegate here.
* ``match_markets`` — compute cross-venue matches from two live market lists.
* ``scan_cross_venue`` / ``scan_bundle`` — signal detectors over live data.

The matcher/signal detectors here are the *real* (computed) versions used by the
live engine; the mock engine keeps curated fixtures. The edge math is shared.
"""

from __future__ import annotations

import re
from typing import Callable

from .models import (
    ExecutionEstimate,
    Leg,
    Market,
    MatchedPair,
    OrderbookSnapshot,
    Opportunity,
    OpportunityKind,
    Side,
    Venue,
)

BookGetter = Callable[[str, str], "OrderbookSnapshot | None"]

# Default cost model. The live engine can override per venue.
DEFAULT_FEE_BPS = {Venue.POLYMARKET: 0.0, Venue.KALSHI: 0.07}
DEFAULT_GAS_USD = {Venue.POLYMARKET: 0.35, Venue.KALSHI: 0.0}


def estimate_realizable_edge(
    legs: list[Leg],
    size_usd: float,
    book_getter: BookGetter,
    fee_bps: dict[Venue, float] | None = None,
    gas_usd: dict[Venue, float] | None = None,
) -> ExecutionEstimate:
    """Simulate filling ``legs`` for ``size_usd`` against live depth.

    Returns edge NET of fees + gas + slippage (never gross). Shared by the mock
    and live engines — the only difference is where ``book_getter`` reads from.
    """
    fee_bps = fee_bps or DEFAULT_FEE_BPS
    gas_usd = gas_usd or DEFAULT_GAS_USD

    fillable = 0.0
    weighted_price = 0.0
    fees = 0.0
    gas = 0.0
    slippage = 0.0
    gross_price_ref = 0.0

    per_leg_budget = size_usd / max(1, len(legs))
    for leg in legs:
        book = book_getter(leg.venue.value, leg.market_id)
        if not book:
            continue
        levels = book.yes_asks if leg.side == Side.YES else book.yes_bids
        if not levels:
            continue
        ref = levels[0].price
        gross_price_ref += ref
        remaining = per_leg_budget
        filled_here = 0.0
        cost = 0.0
        for lvl in levels:
            take = min(remaining, lvl.size_usd)
            if take <= 0:
                break
            cost += take * lvl.price
            filled_here += take
            remaining -= take
        if filled_here <= 0:
            continue
        avg = cost / filled_here
        weighted_price += avg * filled_here
        fillable += filled_here
        slippage += abs(avg - ref) * filled_here
        gas += gas_usd.get(leg.venue, 0.0)
        fees += filled_here * fee_bps.get(leg.venue, 0.0) * 0.1

    avg_fill = (weighted_price / fillable) if fillable else 0.0
    gross_edge = round(len(legs) - gross_price_ref, 4) if gross_price_ref else 0.0
    net = fillable - fees - gas - slippage
    realizable = round((net / size_usd) if size_usd else 0.0, 4)

    return ExecutionEstimate(
        requested_size_usd=round(size_usd, 2),
        fillable_size_usd=round(fillable, 2),
        avg_fill_price=round(avg_fill, 4),
        gross_edge=gross_edge,
        fees_usd=round(fees, 2),
        gas_usd=round(gas, 2),
        slippage_usd=round(slippage, 2),
        realizable_edge=realizable,
    )


# --- matching (real, computed) ----------------------------------------------
_STOPWORDS = {
    "the", "will", "win", "above", "below", "and", "for", "usd", "at", "on",
    "in", "close", "end", "dec", "jan", "eoy",
}


def _tokens(text: str) -> set[str]:
    # Word/number tokens only — strips $, commas, ?, etc. so "$100,000" and
    # "2026?" normalize to comparable tokens.
    return {
        t for t in re.findall(r"[a-z0-9]+", text.lower())
        if len(t) > 2 and t not in _STOPWORDS
    }


def title_similarity(a: str, b: str) -> float:
    """Jaccard token similarity in [0, 1]. Cheap proxy for a real matcher."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def match_markets(
    markets: list[Market], min_confidence: float = 0.35
) -> list[MatchedPair]:
    """Pair markets from different venues that look like the same event.

    # TODO: upgrade to embedding-based matching; token Jaccard is a placeholder.
    """
    pairs: list[MatchedPair] = []
    for i, a in enumerate(markets):
        for b in markets[i + 1:]:
            if a.venue == b.venue:
                continue
            conf = round(title_similarity(a.title, b.title), 3)
            if conf < min_confidence:
                continue
            pairs.append(MatchedPair(event=a.title, confidence=conf, a=a, b=b))
    pairs.sort(key=lambda p: p.confidence, reverse=True)
    return pairs


def best_match(markets: list[Market], event: str, min_confidence: float = 0.2) -> MatchedPair | None:
    """Best cross-venue pair whose event text matches the query."""
    scored: list[tuple[float, MatchedPair]] = []
    for pair in match_markets(markets, min_confidence=min_confidence):
        q = title_similarity(event, pair.a.title) + title_similarity(event, pair.b.title)
        if q > 0:
            scored.append((q * pair.confidence, pair))
    if not scored:
        return None
    scored.sort(key=lambda s: s[0], reverse=True)
    return scored[0][1]


# --- signal detectors (real, computed) --------------------------------------
def scan_cross_venue(
    pairs: list[MatchedPair],
    min_edge: float,
    category: str | None = None,
    cost_haircut: float = 0.008,
) -> list[Opportunity]:
    out: list[Opportunity] = []
    for pair in pairs:
        a, b = pair.a, pair.b
        if category and (a.category or "") != category:
            continue
        net = round(abs(a.yes_price - b.yes_price) - cost_haircut, 4)
        if net < min_edge:
            continue
        cheap, dear = (a, b) if a.yes_price < b.yes_price else (b, a)
        out.append(Opportunity(
            kind=OpportunityKind.CROSS_VENUE,
            title=pair.event,
            category=a.category,
            realizable_edge=net,
            max_size_usd=round(min(a.volume_usd or 0, b.volume_usd or 0) * 0.01, 2),
            legs=[
                Leg(venue=cheap.venue, market_id=cheap.market_id, side=Side.YES),
                Leg(venue=dear.venue, market_id=dear.market_id, side=Side.NO),
            ],
        ))
    return out


def scan_bundle(
    markets: list[Market],
    min_edge: float,
    category: str | None = None,
    cost_haircut: float = 0.004,
) -> list[Opportunity]:
    out: list[Opportunity] = []
    for m in markets:
        if category and (m.category or "") != category:
            continue
        net = round(abs(1.0 - (m.yes_price + m.no_price)) - cost_haircut, 4)
        if net < min_edge:
            continue
        out.append(Opportunity(
            kind=OpportunityKind.BUNDLE,
            title=f"{m.title} (YES/NO bundle mispricing)",
            category=m.category,
            realizable_edge=net,
            max_size_usd=round((m.volume_usd or 0) * 0.005, 2),
            legs=[
                Leg(venue=m.venue, market_id=m.market_id, side=Side.YES),
                Leg(venue=m.venue, market_id=m.market_id, side=Side.NO),
            ],
        ))
    return out
