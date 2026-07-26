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

import math
import os
import re
from difflib import SequenceMatcher
from typing import Callable

from .fairvalue import consensus
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

# --- cost model -------------------------------------------------------------
# Every number here is operator-overridable via env, because venue fee schedules
# change and a stale constant silently corrupts the one number this product
# sells (realizable edge). Defaults reflect the published schedules; re-check
# them at launch with `python deploy/preflight.py` and the venue's fee page.
#
#   KALSHI_FEE_RATE     taker fee coefficient (default 0.07 per the published
#                       formula fee = ceil(rate * C * P * (1-P)))
#   POLYMARKET_FEE_RATE Polymarket charges no base trading fee (default 0.0);
#                       set it if they introduce one on the markets you trade.
#   GAS_USD_POLYMARKET  on-chain settlement cost per leg (Polygon; default 0.05 —
#                       most flow is relayed/gasless, so this is a conservative
#                       non-zero placeholder rather than the old 0.35 guess)
#   GAS_USD_KALSHI      0 (custodial venue, no chain write)


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def gas_for(venue: Venue) -> float:
    """Per-leg settlement/gas cost in USD for a venue."""
    if venue == Venue.POLYMARKET:
        return _env_float("GAS_USD_POLYMARKET", 0.05)
    if venue == Venue.KALSHI:
        return _env_float("GAS_USD_KALSHI", 0.0)
    return _env_float("GAS_USD_DEFAULT", 0.0)


# NOTE: deliberately NOT materialized into a module-level dict — a snapshot
# taken at import time would freeze the env values for the process lifetime,
# silently ignoring runtime overrides. Callers either pass an explicit
# override mapping or let each leg resolve through gas_for() at call time.


def venue_fee(venue: Venue, notional_usd: float, price: float,
              maker: bool = False) -> float:
    """Per-venue trading fee on a fill, in USD.

    **Kalshi** publishes ``fee = ceil(rate * C * P * (1-P))`` per trade, charged
    to the TAKER only (resting maker orders trade free). With ``C ≈ notional /
    P`` this reduces to ``rate * notional * (1-P)`` — a genuinely price-dependent
    cost that peaks near 50c and vanishes at the tails, which a flat-bps model
    gets badly wrong. The published per-trade rounding is UP to the next cent, so
    we ceil: understating a real cost is the one error this product must not make.

    **Polymarket** charges no base trading fee; its cost is on-chain settlement,
    accounted separately as gas.

    ``maker=True`` prices a resting quote (the K5 maker advisor's case): no taker
    fee applies. That is why spread capture there is quoted gross of fees.
    """
    if maker:
        return 0.0  # resting liquidity pays no taker fee on either venue
    if venue == Venue.KALSHI:
        rate = _env_float("KALSHI_FEE_RATE", 0.07)
        raw = rate * max(0.0, notional_usd) * (1.0 - min(1.0, max(0.0, price)))
        return math.ceil(raw * 100.0) / 100.0  # venue rounds each trade up to a cent
    if venue == Venue.POLYMARKET:
        rate = _env_float("POLYMARKET_FEE_RATE", 0.0)
        return round(rate * max(0.0, notional_usd), 4) if rate else 0.0
    return 0.0


def _basket_payoff(legs: list[Leg]) -> float | None:
    """Guaranteed payout per basket unit for the structures the scanner emits.

    * mixed YES+NO pairs (bundle / cross-venue / entailment): exactly one side
      pays -> 1 per unit;
    * all-YES over an exclusive group (dutch under-round): one outcome pays -> 1;
    * all-NO over N outcomes (dutch over-round): N-1 losers pay -> N-1;
    * a single leg has NO guaranteed payout -> None (edge is just cost drag).
    """
    if len(legs) < 2:
        return None
    if all(l.side == Side.NO for l in legs):
        return float(len(legs) - 1)
    return 1.0


def estimate_realizable_edge(
    legs: list[Leg],
    size_usd: float,
    book_getter: BookGetter,
    fee_bps: dict[Venue, float] | None = None,  # kept for back-compat; unused
    gas_usd: dict[Venue, float] | None = None,
) -> ExecutionEstimate:
    """Simulate filling ``legs`` for ``size_usd`` against live depth.

    Contract-based basket math: each leg's walk yields an average YES price and a
    contract count; a NO leg's cost is the YES-complement (1 - avg bid). A basket
    is executable only in COMPLETE units, so units = the thinnest leg's contracts,
    capital = units x unit cost, and profit = units x (guaranteed payout - unit
    cost) - fees - gas. ``realizable_edge`` is profit over deployed capital —
    net of fees, gas and the slippage already embedded in the walked prices.
    A single naked leg has no guaranteed payout: its "edge" is the pure execution
    drag (negative), never a fantasy return. Shared by mock and live engines.
    """
    weighted_price = 0.0
    filled_total = 0.0
    fees = 0.0
    gas = 0.0
    slippage = 0.0
    unit_cost = 0.0       # sum of per-leg YES-equivalent entry prices
    ref_unit_cost = 0.0   # same at top-of-book (pre-slippage), for gross_edge
    leg_contracts: list[float] = []
    filled_legs = 0
    breakdown: dict[str, dict[str, float]] = {}

    per_leg_budget = size_usd / max(1, len(legs))
    for leg in legs:
        book = book_getter(leg.venue.value, leg.market_id)
        if not book:
            continue
        levels = book.yes_asks if leg.side == Side.YES else book.yes_bids
        if not levels:
            continue
        ref = levels[0].price
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
        if filled_here <= 0 or cost <= 0:
            continue
        avg = cost / filled_here
        leg_fee = venue_fee(leg.venue, filled_here, avg)
        # Resolve gas at call time (env-overridable) unless the caller passed an
        # explicit per-venue override — an import-time constant would freeze a
        # stale cost for the process lifetime.
        leg_gas = gas_usd[leg.venue] if gas_usd and leg.venue in gas_usd else gas_for(leg.venue)
        weighted_price += avg * filled_here
        filled_total += filled_here
        slippage += abs(avg - ref) * filled_here
        gas += leg_gas
        fees += leg_fee
        filled_legs += 1
        leg_contracts.append(filled_here / avg)
        # YES-equivalent entry: a NO leg bought against the bid costs 1 - bid.
        unit_cost += avg if leg.side == Side.YES else (1.0 - avg)
        ref_unit_cost += ref if leg.side == Side.YES else (1.0 - ref)
        v = breakdown.setdefault(leg.venue.value, {"fees_usd": 0.0, "gas_usd": 0.0, "filled_usd": 0.0})
        v["fees_usd"] = round(v["fees_usd"] + leg_fee, 4)
        v["gas_usd"] = round(v["gas_usd"] + leg_gas, 4)
        v["filled_usd"] = round(v["filled_usd"] + filled_here, 2)

    avg_fill = (weighted_price / filled_total) if filled_total else 0.0
    payoff = _basket_payoff(legs)

    if payoff is not None and filled_legs < len(legs):
        # A basket with a missing/unfillable leg is NOT executable as a basket —
        # reporting the filled legs alone would sell a broken arb as fillable.
        realizable, gross, fillable = 0.0, 0.0, 0.0
    elif payoff is None or filled_legs == 0:
        # Naked leg (or nothing filled): no guaranteed payout to claim. The honest
        # number is the execution drag on what filled — costs, never invented profit.
        drag = fees + gas + slippage
        realizable = round(-(drag / filled_total), 4) if filled_total else 0.0
        gross = 0.0
        fillable = filled_total
    else:
        units = min(leg_contracts)                 # complete basket units only
        capital = units * unit_cost
        profit = units * (payoff - unit_cost) - fees - gas
        realizable = round(profit / capital, 4) if capital > 0 else 0.0
        # Gross is expressed in the SAME unit as realizable — return on deployed
        # capital, measured at top-of-book before costs. Reporting it as an
        # absolute per-unit difference put two incomparable quantities side by
        # side in one response, and an agent comparing them (the obvious thing to
        # do) was misled whenever unit cost strayed from 1 (audit INV-006).
        # Their difference is now exactly the execution drag.
        gross = round((payoff - ref_unit_cost) / ref_unit_cost, 4) if ref_unit_cost > 0 else 0.0
        fillable = capital

    return ExecutionEstimate(
        requested_size_usd=round(size_usd, 2),
        # Round DOWN: rounding to the nearest cent could report a fillable size
        # slightly ABOVE the requested one, breaking the invariant an agent
        # would naturally assert and over-stating executable size (INV-012).
        fillable_size_usd=math.floor(fillable * 100.0) / 100.0,
        avg_fill_price=round(avg_fill, 4),
        gross_edge=gross,
        fees_usd=round(fees, 2),
        gas_usd=round(gas, 2),
        slippage_usd=round(slippage, 2),
        realizable_edge=realizable,
        cost_breakdown=breakdown,
    )


# --- matching (real, computed) ----------------------------------------------
# A lexical fuzzy matcher: normalize numbers/units/months + drop stopwords, then
# combine token-set Jaccard, a difflib sequence ratio, and numeric agreement.
# Fully offline. # TODO: swap in embedding similarity when a model is available.
_STOPWORDS = {
    "the", "will", "win", "won", "above", "below", "over", "under", "and",
    "for", "usd", "at", "on", "in", "of", "to", "by", "close", "closes",
    "closing", "end", "eoy", "be", "a", "an", "is", "market", "price",
}
_MONTHS = {
    "jan": "01", "january": "01", "feb": "02", "february": "02", "mar": "03",
    "march": "03", "apr": "04", "april": "04", "may": "05", "jun": "06",
    "june": "06", "jul": "07", "july": "07", "aug": "08", "august": "08",
    "sep": "09", "sept": "09", "september": "09", "oct": "10", "october": "10",
    "nov": "11", "november": "11", "dec": "12", "december": "12",
}
_NUM_RE = re.compile(r"\$?\s*(\d[\d,]*\.?\d*)\s*([kmb])?", re.IGNORECASE)


# Beyond any real prediction-market threshold; larger values are garbage or an
# attack and must not reach int() (Python caps int<->str conversion length, and
# float() of a very long digit string yields inf).
_MAX_CANON = 1e15


def _canon_number(digits: str, suffix: str | None) -> str:
    """'$100k' / '100,000' -> '100000'; '1.5m' -> '1500000'.

    TOTAL by contract: market titles are untrusted venue text (on some venues
    anyone can create a market with an arbitrary question), and this runs on
    every title in the scan path. It must never raise for any input — a single
    malformed title previously killed find_mispricing for every caller with an
    OverflowError (audit INV-004). Unconvertible or absurd spans are returned
    unchanged, which simply makes the title not match a threshold ladder.
    """
    try:
        value = float(digits.replace(",", ""))
    except (ValueError, OverflowError):
        return digits
    if not math.isfinite(value) or abs(value) > _MAX_CANON:
        return digits
    mult = {"k": 1e3, "m": 1e6, "b": 1e9}.get((suffix or "").lower(), 1.0)
    scaled = value * mult
    if not math.isfinite(scaled) or abs(scaled) > _MAX_CANON:
        return digits
    try:
        return str(int(round(scaled)))
    except (ValueError, OverflowError):  # belt and braces: never raise upward
        return digits


def _normalize(text: str) -> str:
    text = text.lower()
    # Canonicalize numeric/currency spans first (so $100k == 100,000).
    text = _NUM_RE.sub(lambda m: " " + _canon_number(m.group(1), m.group(2)) + " ", text)
    # Month names -> numeric month.
    words = re.findall(r"[a-z0-9]+", text)
    return " ".join(_MONTHS.get(w, w) for w in words)


def _tokens(text: str) -> set[str]:
    return {
        t for t in _normalize(text).split()
        if (t.isdigit() or len(t) > 2) and t not in _STOPWORDS
    }


def _numbers(text: str) -> set[str]:
    return {t for t in _normalize(text).split() if t.isdigit() and len(t) >= 3}


def title_similarity(a: str, b: str) -> float:
    """Combined lexical similarity in [0, 1].

    0.5 * token Jaccard + 0.35 * difflib sequence ratio + 0.15 * number overlap.
    Number normalization means '$100k' and '$100,000' reinforce a match.
    """
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    jaccard = len(ta & tb) / len(ta | tb)
    ratio = SequenceMatcher(None, _normalize(a), _normalize(b)).ratio()
    na, nb = _numbers(a), _numbers(b)
    num_overlap = (len(na & nb) / min(len(na), len(nb))) if (na and nb) else 0.0
    return round(0.5 * jaccard + 0.35 * ratio + 0.15 * num_overlap, 3)


def semantic_similarity(a: str, b: str) -> float | None:
    """Cosine similarity of title embeddings, or None if no embedder is available.

    Resolves the lexical tier's "shared entity + shared year" false positives
    (e.g. 'Ethereum flips Bitcoin 2026' vs 'Bitcoin above 100k 2026'), which
    share tokens but mean different events.
    """
    from .embeddings import cosine, embed_texts

    if not a.strip() or not b.strip():
        return 0.0
    vectors = embed_texts([a, b])  # cached: each title embedded once
    if vectors is None:
        return None  # no embedder available
    return round(cosine(vectors[0], vectors[1]), 3)


def similarity(a: str, b: str) -> float:
    """Title similarity under the configured matcher tier.

      MATCHER=lexical  (default) — token/difflib/number blend (offline)
      MATCHER=semantic          — embedding cosine (falls back to lexical if the
                                  embedder is unavailable)
      MATCHER=hybrid            — 0.5 * lexical + 0.5 * semantic

    Note: semantic cosine sits on a different scale than the lexical blend; tune
    the match threshold via MATCH_MIN_CONFIDENCE when using semantic/hybrid.
    """
    mode = os.getenv("MATCHER", "lexical").lower()
    lex = title_similarity(a, b)
    if mode == "lexical":
        return lex
    sem = semantic_similarity(a, b)
    if sem is None:
        return lex  # embedder unavailable -> degrade to lexical
    if mode == "semantic":
        return sem
    return round(0.5 * lex + 0.5 * sem, 3)  # hybrid


def _default_min_confidence() -> float:
    raw = os.getenv("MATCH_MIN_CONFIDENCE")
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    return 0.45


def match_markets(
    markets: list[Market], min_confidence: float | None = None
) -> list[MatchedPair]:
    """Pair markets from different venues that look like the same event.

    Uses the configured matcher tier (``similarity``). The default threshold
    favors PRECISION: a false pair produces a fake arbitrage opportunity, which
    is worse for trust than missing a real one. Override the threshold with
    MATCH_MIN_CONFIDENCE (recommended when switching to semantic/hybrid, whose
    cosine scores sit on a different scale than the lexical blend).
    """
    threshold = _default_min_confidence() if min_confidence is None else min_confidence
    pairs: list[MatchedPair] = []
    for i, a in enumerate(markets):
        for b in markets[i + 1:]:
            if a.venue == b.venue:
                continue
            conf = round(similarity(a.title, b.title), 3)
            if conf < threshold:
                continue
            pairs.append(MatchedPair(event=a.title, confidence=conf, a=a, b=b))
    pairs.sort(key=lambda p: p.confidence, reverse=True)
    return pairs


def best_match(markets: list[Market], event: str, min_confidence: float = 0.2) -> MatchedPair | None:
    """Best cross-venue pair whose event text matches the query."""
    scored: list[tuple[float, MatchedPair]] = []
    for pair in match_markets(markets, min_confidence=min_confidence):
        q = similarity(event, pair.a.title) + similarity(event, pair.b.title)
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
            fair_value=consensus([a, b]),  # consensus anchor for the spread
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


def scan_dutch_book(
    markets: list[Market],
    min_edge: float,
    category: str | None = None,
    cost_haircut: float = 0.01,
) -> list[Opportunity]:
    """Combinatorial arbitrage over mutually-exclusive outcome groups.

    Markets sharing an ``event_group`` are complementary outcomes whose YES
    prices should sum to ~1. If the sum is meaningfully below 1, buy YES on every
    outcome for a guaranteed payout > cost; if above 1, buy NO on every outcome.
    This is where non-obvious edge lives and is hard to replicate.
    """
    groups: dict[str, list[Market]] = {}
    for m in markets:
        if not m.event_group:
            continue
        if category and (m.category or "") != category:
            continue
        groups.setdefault(m.event_group, []).append(m)

    out: list[Opportunity] = []
    for group, members in groups.items():
        if len(members) < 2:
            continue
        total_yes = sum(m.yes_price for m in members)
        # Under-round: buy every YES (pay total_yes, collect exactly 1).
        # Over-round: buy every NO (pay len - total_yes, collect len - 1).
        under = round((1.0 - total_yes) - cost_haircut, 4)
        over = round((total_yes - 1.0) - cost_haircut, 4)
        if under >= min_edge:
            net, side = under, Side.YES
        elif over >= min_edge:
            net, side = over, Side.NO
        else:
            continue
        cap = min((m.volume_usd or 0) for m in members) * 0.01
        out.append(Opportunity(
            kind=OpportunityKind.DUTCH_BOOK,
            title=f"Dutch book across '{group}' ({len(members)} outcomes, "
                  f"YES sum {round(total_yes, 3)})",
            category=members[0].category,
            realizable_edge=net,
            max_size_usd=round(cap, 2),
            legs=[Leg(venue=m.venue, market_id=m.market_id, side=side) for m in members],
        ))
    return out


def annotate_risk(
    opp: Opportunity,
    close_time,
    resolution_risk: float = 0.02,
) -> Opportunity:
    """Attach holding period, annualized edge, and a resolution-risk haircut.

    An edge that locks capital until a distant resolution is worth less than the
    same edge resolving next week — annualized return is the honest comparison.
    """
    from datetime import datetime, timezone

    if close_time is not None:
        now = datetime.now(timezone.utc)
        days = max(0.5, (close_time - now).total_seconds() / 86400.0)
        opp.holding_days = round(days, 2)
        opp.annualized_edge = round(opp.realizable_edge / days * 365.0, 4)
    opp.resolution_risk = resolution_risk
    return opp


def _latest_close(opp: Opportunity, close_by_id: dict) -> object | None:
    """Latest close time across ALL of an opportunity's legs.

    A multi-leg position's edge is fully realized only when its LAST leg
    resolves — capital stays committed until then. Using leg[0] (the old
    behavior) or the earliest close would understate the holding period and
    OVERSTATE annualized edge; the latest close is the honest, conservative
    basis.
    """
    closes = [
        close_by_id.get(l.market_id)
        for l in opp.legs
        if close_by_id.get(l.market_id) is not None
    ]
    return max(closes) if closes else None


def _cap_size_by_depth(opp: Opportunity, book_getter: BookGetter) -> None:
    """Cap ``max_size_usd`` by the real top-of-book depth of the tightest leg.

    A volume-derived cap can promise size the book can't actually fill. The
    executable size of a multi-leg position is bounded by its thinnest leg, so we
    shrink ``max_size_usd`` to the minimum available depth across legs (best
    effort — a missing book leaves the volume-based estimate untouched).
    """
    leg_depths: list[float] = []
    for leg in opp.legs:
        book = book_getter(leg.venue.value, leg.market_id)
        if not book:
            return  # can't verify a leg -> don't overstate; keep volume estimate
        levels = book.yes_asks if leg.side == Side.YES else book.yes_bids
        leg_depths.append(sum(l.size_usd for l in levels))
    if leg_depths:
        opp.max_size_usd = round(min(opp.max_size_usd, min(leg_depths)), 2)


def finalize_opportunities(
    opps: list[Opportunity],
    markets: list[Market],
    book_getter: BookGetter | None = None,
) -> list[Opportunity]:
    """Risk-adjust (latest close across all legs) and depth-cap, then sort.

    Shared by the mock and live engines so the risk math lives in one place.
    """
    close_by_id = {m.market_id: m.close_time for m in markets}
    for opp in opps:
        annotate_risk(opp, _latest_close(opp, close_by_id))
        if book_getter is not None:
            _cap_size_by_depth(opp, book_getter)
    opps.sort(key=lambda o: o.realizable_edge, reverse=True)
    return opps
