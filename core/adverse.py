"""Adverse-selection / trap score (J1).

An edge that looks real can still be a trap — you're the one being picked off.
This scores that risk per opportunity from three signals we already compute:

* **informed flow against the position** (microstructure): if the tape is being
  walked the way your entry would lose, someone informed is likely on the other
  side;
* **historical hit-rate of similar edges** (reconciliation, by kind): if edges of
  this kind have historically resolved *against* us, this one probably will too;
* **quote quality** (F7 flags): garbage prices produce phantom edges.

Output: a ``trap_score`` in [0,1], the contributing ``factors``, and a
``trust_adjusted_edge = realizable_edge * (1 - trap_score)`` — the edge you should
actually plan around. Fewer bad fills => a better hit-rate, the server's strongest
marketing asset. Pure/offline; the callers supply the already-computed signals.
"""

from __future__ import annotations

import os

from .models import Opportunity, Side


def assess(
    opp: Opportunity,
    momentum: float | None = None,
    informed_flow: bool = False,
    historical_hit: float | None = None,
    historical_samples: int = 0,
    quality_flags: list[str] | None = None,
) -> dict:
    """Combine the three signals into a trap score for one opportunity."""
    trap = 0.0
    factors: list[str] = []

    # 1. Informed flow moving against the position's primary leg.
    if informed_flow and momentum is not None and opp.legs:
        side = opp.legs[0].side
        against = (side == Side.YES and momentum < 0) or (side == Side.NO and momentum > 0)
        if against:
            weight = round(min(0.4, 0.4 * abs(momentum)), 4)
            trap += weight
            factors.append("informed flow is moving against the position")

    # 2. Edges of this kind historically resolved against us.
    min_samples = int(os.getenv("ADVERSE_MIN_SAMPLES", "10"))
    if historical_hit is not None and historical_samples >= min_samples:
        if historical_hit < 0.5:
            trap += round((0.5 - historical_hit), 4)  # up to +0.5 at a 0% hit-rate
            factors.append(
                f"{opp.kind.value} edges historically hit {historical_hit:.0%} "
                f"(n={historical_samples})")

    # 3. Unreliable quote.
    flags = quality_flags or []
    serious = [f for f in flags if f in ("crossed_book", "stale", "wide_spread")]
    if serious:
        trap += round(min(0.2, 0.08 * len(serious)), 4)
        factors.append("low quote quality: " + ", ".join(serious))

    trap = round(min(1.0, trap), 4)
    return {
        "trap_score": trap,
        "factors": factors,
        "trust_adjusted_edge": round(opp.realizable_edge * (1.0 - trap), 4),
    }
