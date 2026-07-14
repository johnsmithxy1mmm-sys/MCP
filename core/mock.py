"""Realistic mock of the intelligence engine and storage.

Same function signatures as the real `core.matcher` / `core.signals` /
`core.edge` and `storage.repo`, so wiring the real engine later is a drop-in
replacement of this module. Data is deterministic (seeded) so tests and the
MCP Inspector see stable, believable output.

Every public function here is a seam:  # TODO: wire to real core
"""

from __future__ import annotations

import hashlib
import random
from datetime import datetime, timedelta, timezone

from .models import (
    ExecutionEstimate,
    Leg,
    Market,
    MatchedPair,
    OrderbookLevel,
    OrderbookSnapshot,
    Opportunity,
    OpportunityKind,
    PricePoint,
    Side,
    Venue,
    utcnow,
)

# --- Fee / cost model (mock) ------------------------------------------------
# Realistic-ish taker fees and gas. Real engine sources these live per venue.
_FEE_BPS = {Venue.POLYMARKET: 0.0, Venue.KALSHI: 0.07}  # Kalshi ~7% of profit-ish
_GAS_USD = {Venue.POLYMARKET: 0.35, Venue.KALSHI: 0.0}  # onchain gas vs none


# --- Seed catalog -----------------------------------------------------------
# A small believable universe. market_id is stable per venue.
_MARKETS: list[Market] = [
    Market(
        venue=Venue.POLYMARKET,
        market_id="pm-us-election-2028-dem",
        title="Will the Democratic nominee win the 2028 US Presidential election?",
        category="politics",
        yes_price=0.52,
        no_price=0.48,
        volume_usd=4_820_000,
    ),
    Market(
        venue=Venue.KALSHI,
        market_id="kx-pres-2028-dem",
        title="2028 US Presidential Election: Democratic winner",
        category="politics",
        yes_price=0.55,
        no_price=0.45,
        volume_usd=1_310_000,
    ),
    Market(
        venue=Venue.POLYMARKET,
        market_id="pm-btc-100k-2026",
        title="Will Bitcoin close above $100k at end of 2026?",
        category="crypto",
        yes_price=0.71,
        no_price=0.29,
        volume_usd=2_040_000,
    ),
    Market(
        venue=Venue.KALSHI,
        market_id="kx-btc-100k-eoy26",
        title="Bitcoin above $100,000 on Dec 31 2026",
        category="crypto",
        yes_price=0.66,
        no_price=0.34,
        volume_usd=560_000,
    ),
    Market(
        venue=Venue.POLYMARKET,
        market_id="pm-fed-cut-sep",
        title="Will the Fed cut rates at the September meeting?",
        category="economics",
        yes_price=0.40,
        no_price=0.60,
        volume_usd=890_000,
    ),
    Market(
        venue=Venue.KALSHI,
        market_id="kx-fed-sep-cut",
        title="Fed rate cut in September",
        category="economics",
        yes_price=0.43,
        no_price=0.57,
        volume_usd=610_000,
    ),
]

# Cross-venue matches (same real-world event), with confidence.
_MATCHES: list[tuple[str, str, str, float]] = [
    # (event label, polymarket_id, kalshi_id, confidence)
    ("2028 US Presidential election — Democratic winner",
     "pm-us-election-2028-dem", "kx-pres-2028-dem", 0.97),
    ("Bitcoin above $100k at end of 2026",
     "pm-btc-100k-2026", "kx-btc-100k-eoy26", 0.94),
    ("Fed rate cut in September",
     "pm-fed-cut-sep", "kx-fed-sep-cut", 0.90),
]


def _by_id(market_id: str) -> Market | None:
    for m in _MARKETS:
        if m.market_id == market_id:
            return m
    return None


def _seed(*parts: str) -> random.Random:
    h = hashlib.sha256("|".join(parts).encode()).hexdigest()
    return random.Random(int(h[:8], 16))


# ---------------------------------------------------------------------------
# storage.repo — live state (Redis) + history (TimescaleDB) in production
# ---------------------------------------------------------------------------
class _Repo:
    """Mock storage repository.  # TODO: wire to real storage.repo (Redis/Timescale)."""

    def search_markets(
        self,
        query: str,
        category: str | None = None,
        venue: str | None = None,
    ) -> list[Market]:
        q = (query or "").lower().strip()
        out: list[Market] = []
        for m in _MARKETS:
            if category and (m.category or "") != category:
                continue
            if venue and m.venue.value != venue:
                continue
            hay = f"{m.title} {m.category} {m.market_id}".lower()
            if not q or any(tok in hay for tok in q.split()):
                out.append(m)
        return out

    def get_market(self, venue: str, market_id: str) -> Market | None:
        m = _by_id(market_id)
        if m and m.venue.value == venue:
            return m
        return None

    def get_orderbook(self, venue: str, market_id: str) -> OrderbookSnapshot | None:
        m = self.get_market(venue, market_id)
        if not m:
            return None
        rng = _seed("book", venue, market_id)
        mid = m.yes_price
        asks, bids = [], []
        for i in range(5):
            spread = 0.005 * (i + 1)
            asks.append(OrderbookLevel(
                price=round(min(0.99, mid + spread), 4),
                size_usd=round(rng.uniform(200, 3000), 2),
            ))
            bids.append(OrderbookLevel(
                price=round(max(0.01, mid - spread), 4),
                size_usd=round(rng.uniform(200, 3000), 2),
            ))
        return OrderbookSnapshot(
            venue=Venue(venue),
            market_id=market_id,
            as_of=utcnow(),
            yes_asks=asks,
            yes_bids=bids,
        )

    def get_history(
        self, venue: str, market_id: str, frm: datetime, to: datetime
    ) -> list[PricePoint]:
        m = self.get_market(venue, market_id)
        if not m:
            return []
        rng = _seed("hist", venue, market_id)
        if to <= frm:
            return []
        span = to - frm
        # ~ hourly points, capped for token sanity.
        n = max(2, min(168, int(span.total_seconds() // 3600)))
        step = span / n
        price = m.yes_price
        points: list[PricePoint] = []
        for i in range(n + 1):
            price = min(0.99, max(0.01, price + rng.uniform(-0.02, 0.02)))
            points.append(PricePoint(
                ts=frm + step * i,
                yes_price=round(price, 4),
                spread=round(rng.uniform(0.004, 0.02), 4),
            ))
        return points

    def list_venues(self) -> list[dict]:
        counts: dict[str, int] = {}
        for m in _MARKETS:
            counts[m.venue.value] = counts.get(m.venue.value, 0) + 1
        return [
            {
                "venue": v.value,
                "status": "live",
                "markets_covered": counts.get(v.value, 0),
                "categories": sorted({
                    m.category for m in _MARKETS
                    if m.venue == v and m.category
                }),
            }
            for v in Venue
        ]


repo = _Repo()


# ---------------------------------------------------------------------------
# core.matcher
# ---------------------------------------------------------------------------
def match_event(event: str) -> MatchedPair | None:
    """Return the best cross-venue MatchedPair for an event, or None.

    # TODO: wire to real core.matcher (embedding + rules based matching).
    """
    q = (event or "").lower()
    best: tuple[float, MatchedPair] | None = None
    for label, pm_id, kx_id, conf in _MATCHES:
        a, b = _by_id(pm_id), _by_id(kx_id)
        if not a or not b:
            continue
        overlap = _token_overlap(q, label.lower())
        if overlap == 0:
            continue
        pair = MatchedPair(event=label, confidence=conf, a=a, b=b)
        score = overlap * conf
        if best is None or score > best[0]:
            best = (score, pair)
    return best[1] if best else None


def _token_overlap(a: str, b: str) -> int:
    ta = {t for t in a.replace("$", " ").split() if len(t) > 2}
    tb = {t for t in b.replace("$", " ").split() if len(t) > 2}
    return len(ta & tb)


# ---------------------------------------------------------------------------
# core.signals
# ---------------------------------------------------------------------------
def scan_opportunities(
    min_edge: float,
    kind: str | None = None,
    category: str | None = None,
) -> list[Opportunity]:
    """Detect bundle + cross_venue opportunities with realizable edge >= min_edge.

    # TODO: wire to real core.signals (bundle + cross_venue detectors).
    """
    out: list[Opportunity] = []

    # Cross-venue: any matched pair whose spread net of costs beats min_edge.
    for label, pm_id, kx_id, conf in _MATCHES:
        a, b = _by_id(pm_id), _by_id(kx_id)
        if not a or not b:
            continue
        if category and (a.category or "") != category:
            continue
        gross = abs(a.yes_price - b.yes_price)
        # Net edge after a rough cost haircut from both venues.
        haircut = _FEE_BPS[a.venue] * 0.0 + 0.008  # spread/fee/gas approximation
        net = round(gross - haircut, 4)
        if net < min_edge:
            continue
        cheap, dear = (a, b) if a.yes_price < b.yes_price else (b, a)
        out.append(Opportunity(
            kind=OpportunityKind.CROSS_VENUE,
            title=label,
            category=a.category,
            realizable_edge=net,
            max_size_usd=round(min(a.volume_usd or 0, b.volume_usd or 0) * 0.01, 2),
            legs=[
                Leg(venue=cheap.venue, market_id=cheap.market_id, side=Side.YES),
                Leg(venue=dear.venue, market_id=dear.market_id, side=Side.NO),
            ],
        ))

    # Bundle: a single-venue market whose YES+NO deviates from 1.0 enough.
    for m in _MARKETS:
        if category and (m.category or "") != category:
            continue
        book_sum = m.yes_price + m.no_price
        net = round(abs(1.0 - book_sum) - 0.004, 4)
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

    if kind:
        out = [o for o in out if o.kind.value == kind]
    out.sort(key=lambda o: o.realizable_edge, reverse=True)
    return out


# ---------------------------------------------------------------------------
# core.edge
# ---------------------------------------------------------------------------
def realizable_edge(legs: list[Leg], size_usd: float) -> ExecutionEstimate:
    """Simulate filling `legs` for `size_usd` and return realizable edge.

    Walks current mock depth, applies venue fees + gas + slippage.
    # TODO: wire to real core.edge.realizable_edge(...).
    """
    fillable = 0.0
    weighted_price = 0.0
    fees = 0.0
    gas = 0.0
    slippage = 0.0
    gross_price_ref = 0.0

    per_leg_budget = size_usd / max(1, len(legs))
    for leg in legs:
        book = repo.get_orderbook(leg.venue.value, leg.market_id)
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
        gas += _GAS_USD[leg.venue]
        fees += filled_here * _FEE_BPS[leg.venue] * 0.1  # rough taker fee on notional

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
