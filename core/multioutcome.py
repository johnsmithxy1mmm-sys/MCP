"""Native multi-outcome markets — de-vig, complete-set arbitrage (G).

Binary yes/no is the wrong primitive for a race with N candidates or a number
split into brackets: flattening each outcome into an independent yes/no throws
away the fact that they're one event whose probabilities must sum to ~1. This
module reconstructs that structure from the binary markets (grouped by
``event_group``) and exposes what only the full set makes computable:

* **de-vigged probabilities** — strip the venue margin (overround) to get the
  market's 'true' probability per outcome, a data product on its own;
* a **completeness-aware full dutch book** — the correctness win over the binary
  scanner: an over-round set (sum > 1) is *always* arbitrage (buy NO on every
  outcome — at most one wins, so at least N-1 pay), but an under-round set (sum <
  1) is only free money if the outcomes cover EVERY possibility. Flagging an
  under-round on an incomplete set (a two-candidate slice of a real race) is a
  false arbitrage the binary path can emit; here it's gated on ``complete``.

Pure/offline over canonical models.
"""

from __future__ import annotations

from .models import (
    Leg,
    Market,
    MultiOutcomeMarket,
    Opportunity,
    OpportunityKind,
    Outcome,
    Side,
)


def _outcome_name(m: Market) -> str:
    return m.title or m.market_id


def build_from_markets(
    markets: list[Market], complete_groups: set[str] | None = None
) -> list[MultiOutcomeMarket]:
    """Reconstruct multi-outcome markets from binary markets sharing an
    ``event_group``. ``complete_groups`` marks which groups cover all outcomes."""
    complete = complete_groups or set()
    groups: dict[str, list[Market]] = {}
    for m in markets:
        if m.event_group:
            groups.setdefault(m.event_group, []).append(m)
    out: list[MultiOutcomeMarket] = []
    for group, members in groups.items():
        if len(members) < 2:
            continue
        out.append(MultiOutcomeMarket(
            event=group,
            outcomes=[Outcome(name=_outcome_name(m), venue=m.venue, market_id=m.market_id,
                              yes_price=m.yes_price, volume_usd=m.volume_usd) for m in members],
            category=members[0].category,
            close_time=members[0].close_time,
            complete=group in complete,
        ))
    return out


def full_dutch_book(
    mom: MultiOutcomeMarket, min_edge: float = 0.0, cost_haircut: float = 0.01
) -> Opportunity | None:
    """Complete-set dutch book for a multi-outcome market, or None.

    Over-round (Σp > 1): sell the whole book — buy NO on every outcome; guaranteed
    since at most one resolves YES. Under-round (Σp < 1): buy YES on every outcome;
    only valid when the set is COMPLETE (else the missing outcomes hold the gap).
    """
    total = mom.total_probability
    over = round((total - 1.0) - cost_haircut, 4)
    under = round((1.0 - total) - cost_haircut, 4)
    if over >= max(min_edge, 0.0):
        side, edge, note = Side.NO, over, "over-round (buy NO on every outcome)"
    elif mom.complete and under >= max(min_edge, 0.0):
        side, edge, note = Side.YES, under, "under-round (buy YES on every outcome)"
    else:
        return None
    if edge < min_edge:
        return None
    cap = min((o.volume_usd or 0) for o in mom.outcomes) * 0.01
    return Opportunity(
        kind=OpportunityKind.DUTCH_BOOK,
        title=f"Full dutch book '{mom.event}' ({len(mom.outcomes)} outcomes, "
              f"Σp={total:g}) — {note}",
        category=mom.category,
        realizable_edge=round(edge, 4),
        max_size_usd=round(cap, 2),
        legs=[Leg(venue=o.venue, market_id=o.market_id, side=side) for o in mom.outcomes],
    )


def view(mom: MultiOutcomeMarket) -> dict:
    """The ``outcomes://{event}`` payload for a multi-outcome market."""
    arb = full_dutch_book(mom, min_edge=0.0)
    return {
        "event": mom.event,
        "category": mom.category,
        "outcomes": len(mom.outcomes),
        "total_probability": mom.total_probability,
        "overround": mom.overround,
        "complete": mom.complete,
        "favorite": mom.favorite,
        "fair_probabilities": mom.normalized,   # de-vigged
        "dutch_book": None if arb is None else {
            "realizable_edge": arb.realizable_edge,
            "max_size_usd": arb.max_size_usd,
            "title": arb.title,
            "legs": [{"venue": l.venue.value, "market_id": l.market_id, "side": l.side.value}
                     for l in arb.legs],
        },
        "note": ("Under-round arbitrage is only valid if `complete` is true; an "
                 "incomplete set's gap is held by the unlisted outcomes."),
    }
