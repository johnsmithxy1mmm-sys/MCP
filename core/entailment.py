"""Logical-entailment + term-structure arbitrage (C1).

Spread scanners only see two markets quoting the *same* question. But markets are
also bound by **logic**, and those bounds are violated far more often than raw
spreads — because no one prices the whole ladder jointly:

* **Threshold monotonicity** (same entity, same date): P(BTC > 150k) ≤ P(BTC >
  100k). A higher bar can never be more likely than a lower one. If it is, that's
  a *risk-free* arbitrage invisible to every price-difference scanner.
* **Term structure** (same entity, same threshold, cumulative "by date" framing):
  P(reaches X by June) ≤ P(reaches X by December). Once it happens it stays
  happened, so probability must be non-decreasing in the deadline.

A violation is arbitrage because the implication holds at *resolution* regardless
of the outcome: sell the YES that's too expensive, buy the YES that's too cheap;
whenever the expensive one pays, the cheap one pays too. This is the hardest
signal to replicate — it needs a parser that turns titles into a partial order,
not just a string match — and it doubles as a data product: the implied
**probability term structure** of an event.

Pure/offline. Reuses the number/date normalization from ``algorithms``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .algorithms import _canon_number, _MONTHS, _STOPWORDS
from .models import Leg, Market, Opportunity, OpportunityKind, Side

# Comparator direction: does the market pay when the level is ABOVE or BELOW?
_ABOVE = re.compile(r"\b(above|over|exceed(?:s)?|greater|more than|at least|>=?|reach(?:es)?|hit(?:s)?|top(?:s)?)\b", re.I)
_BELOW = re.compile(r"\b(below|under|less than|fewer|at most|<=?|drop(?:s)? below|dip(?:s)?)\b", re.I)
# "Cumulative" framing (once true, stays true) makes date-monotonicity valid.
_CUMULATIVE = re.compile(r"\b(by|before|anytime|ever|reach(?:es)?|hit(?:s)?|at any point)\b", re.I)
_YEAR = re.compile(r"\b(20[2-4]\d)\b")
_MONEY = re.compile(r"\$\s*(\d[\d,]*\.?\d*)\s*([kmb])?", re.I)
_NUMBER = re.compile(r"\b(\d[\d,]*\.?\d*)\s*([kmb])?\b", re.I)
_CURRENCY_WORDS = {"usd", "dollar", "dollars", "price", "close", "closes", "closing"}


@dataclass(frozen=True)
class Claim:
    """A parsed 'entity compared to threshold by deadline' statement."""

    entity: str
    direction: str  # "above" | "below"
    threshold: float
    year: int | None
    month: str | None
    cumulative: bool
    market: Market

    @property
    def deadline_key(self) -> tuple:
        return (self.year, self.month)


def _direction(title: str) -> str | None:
    if _ABOVE.search(title):
        return "above"
    if _BELOW.search(title):
        return "below"
    return None


def _threshold(title: str) -> float | None:
    """Extract the comparison level, preferring an explicit $-amount and never
    mistaking a 4-digit year for the threshold."""
    m = _MONEY.search(title)
    if m:
        return float(_canon_number(m.group(1), m.group(2)))
    best: float | None = None
    for m in _NUMBER.finditer(title):
        raw, suffix = m.group(1), m.group(2)
        digits = raw.replace(",", "")
        # Skip bare 4-digit years (they're deadlines, not thresholds).
        if not suffix and digits.isdigit() and 2020 <= int(digits) <= 2049 and "." not in raw:
            continue
        value = float(_canon_number(raw, suffix))
        if best is None or value > best:
            best = value
    return best


def _entity(title: str, threshold: float | None) -> str:
    """Normalized entity tokens: strip comparators, numbers, months, currency,
    stopwords — what's left names the underlying event subject."""
    lowered = title.lower()
    lowered = _ABOVE.sub(" ", lowered)
    lowered = _BELOW.sub(" ", lowered)
    # Strip money/number spans (incl. k/m/b-suffixed like "100k") BEFORE tokenizing
    # so the threshold value can't leak into the entity and split a real ladder.
    lowered = _MONEY.sub(" ", lowered)
    lowered = _NUMBER.sub(" ", lowered)
    words = re.findall(r"[a-z0-9]+", lowered)
    keep = []
    for w in words:
        if w.isdigit():
            continue
        if re.fullmatch(r"\d[\d,]*\.?\d*[kmb]?", w):  # residual numeric token
            continue
        if w in _MONTHS or w in _STOPWORDS or w in _CURRENCY_WORDS:
            continue
        if w in ("k", "m", "b", "eoy", "end", "year"):
            continue
        if len(w) <= 1:
            continue
        keep.append(w)
    return " ".join(sorted(set(keep)))


def parse_claim(market: Market) -> Claim | None:
    """Parse a market title into a comparable Claim, or None if it isn't a
    threshold statement we can order."""
    title = market.title or ""
    direction = _direction(title)
    threshold = _threshold(title)
    if direction is None or threshold is None:
        return None
    year_m = _YEAR.search(title)
    year = int(year_m.group(1)) if year_m else None
    month = None
    for token, num in _MONTHS.items():
        if re.search(rf"\b{token}\b", title, re.I):
            month = num
            break
    entity = _entity(title, threshold)
    if not entity:
        return None
    return Claim(
        entity=entity,
        direction=direction,
        threshold=threshold,
        year=year,
        month=month,
        cumulative=bool(_CUMULATIVE.search(title)),
        market=market,
    )


def _entail_leg(claim: Claim, side: Side) -> Leg:
    m = claim.market
    return Leg(venue=m.venue, market_id=m.market_id, side=side)


def _make_opp(title: str, expensive: Claim, cheap: Claim, edge: float) -> Opportunity:
    """Sell the YES that's too dear, buy the YES that's too cheap.

    Whenever `expensive` resolves YES, `cheap` does too (that's the entailment),
    so the position is non-negative at resolution and banks the mispricing now.
    """
    return Opportunity(
        kind=OpportunityKind.ENTAILMENT,
        title=title,
        category=expensive.market.category,
        realizable_edge=round(edge, 4),
        max_size_usd=round(
            min(expensive.market.volume_usd or 0, cheap.market.volume_usd or 0) * 0.01, 2
        ),
        fair_value=None,
        legs=[
            _entail_leg(expensive, Side.NO),   # short the over-priced stronger claim
            _entail_leg(cheap, Side.YES),      # long the under-priced weaker claim
        ],
    )


def scan_entailment(
    markets: list[Market], min_edge: float, cost_haircut: float = 0.01
) -> list[Opportunity]:
    """Detect threshold- and term-structure violations across `markets`."""
    claims = [c for c in (parse_claim(m) for m in markets) if c is not None]
    out: list[Opportunity] = []
    out += _scan_threshold(claims, min_edge, cost_haircut)
    out += _scan_term_structure(claims, min_edge, cost_haircut)
    return out


def _scan_threshold(claims: list[Claim], min_edge: float, haircut: float) -> list[Opportunity]:
    """Same entity + direction + deadline, different thresholds must be monotone."""
    groups: dict[tuple, list[Claim]] = {}
    for c in claims:
        groups.setdefault((c.entity, c.direction, c.deadline_key), []).append(c)

    out: list[Opportunity] = []
    for (entity, direction, _), members in groups.items():
        if len(members) < 2:
            continue
        members = sorted(members, key=lambda c: c.threshold)
        for i in range(len(members) - 1):
            lo, hi = members[i], members[i + 1]
            if lo.threshold == hi.threshold:
                continue
            p_lo = lo.market.yes_price
            p_hi = hi.market.yes_price
            if direction == "above":
                # Higher bar must be LESS likely: P(hi) <= P(lo).
                edge = round((p_hi - p_lo) - haircut, 4)
                expensive, cheap = hi, lo
            else:
                # "below": higher bar must be MORE likely: P(hi) >= P(lo).
                edge = round((p_lo - p_hi) - haircut, 4)
                expensive, cheap = lo, hi
            if edge < min_edge:
                continue
            out.append(_make_opp(
                f"Entailment: {entity} {direction} "
                f"{cheap.threshold:g} vs {hi.threshold if direction=='above' else lo.threshold:g} "
                f"(implication violated by {edge:g})",
                expensive, cheap, edge,
            ))
    return out


def _scan_term_structure(claims: list[Claim], min_edge: float, haircut: float) -> list[Opportunity]:
    """Same entity + threshold, cumulative 'above/reach by DATE' must rise with date."""
    groups: dict[tuple, list[Claim]] = {}
    for c in claims:
        if c.direction != "above" or not c.cumulative or c.year is None:
            continue
        groups.setdefault((c.entity, c.direction, c.threshold), []).append(c)

    out: list[Opportunity] = []
    for (entity, _, threshold), members in groups.items():
        if len(members) < 2:
            continue
        members = sorted(members, key=lambda c: (c.year, c.month or "00"))
        for i in range(len(members) - 1):
            early, late = members[i], members[i + 1]
            if early.deadline_key == late.deadline_key:
                continue
            # Cumulative: later deadline must be at least as likely as earlier.
            edge = round((early.market.yes_price - late.market.yes_price) - haircut, 4)
            if edge < min_edge:
                continue
            out.append(_make_opp(
                f"Term-structure: {entity} reach {threshold:g} by "
                f"{early.deadline_key} priced above by {late.deadline_key} "
                f"(violation {edge:g})",
                early, late, edge,
            ))
    return out
