"""Position sizing: Kelly fraction + market-impact curve (B4).

``estimate_execution`` already answers "what's my edge at this size?". Sizing
answers the next two questions an agent actually has:

* **How much should I bet?** — the Kelly-optimal fraction of bankroll for a
  binary market bet, scaled by a fractional-Kelly multiplier (half-Kelly by
  default; full Kelly is famously too aggressive under estimation error).
* **What happens if I size up?** — the market-impact curve: average fill price
  and realizable edge as the position grows, walked against real book depth, so
  the agent sees where the edge decays to zero.

Pure/offline: operates on legs + a ``book_getter`` (the same seam the edge model
uses), so it works identically for the mock and live engines.

  KELLY_FRACTION   fractional-Kelly multiplier (default 0.5 = half-Kelly)
"""

from __future__ import annotations

import os

from .algorithms import BookGetter, estimate_realizable_edge
from .models import Leg


def kelly_fraction(fair_prob: float, price: float, fractional: float | None = None) -> float:
    """Fractional-Kelly stake for buying YES at ``price`` given ``fair_prob``.

    Full Kelly for a binary bet paying $1 is ``(q - p) / (1 - p)`` (edge over the
    amount at risk). We scale by a fractional multiplier and clamp to [0, 1]. A
    non-positive edge (or a degenerate price) sizes to zero — never bet into a
    negative edge.
    """
    if fractional is None:
        fractional = float(os.getenv("KELLY_FRACTION", "0.5"))
    if not (0.0 < price < 1.0) or not (0.0 <= fair_prob <= 1.0):
        return 0.0
    edge = fair_prob - price
    if edge <= 0:
        return 0.0
    full = edge / (1.0 - price)
    return round(max(0.0, min(1.0, full * fractional)), 4)


def impact_curve(
    legs: list[Leg],
    base_size_usd: float,
    book_getter: BookGetter,
    multiples: tuple[float, ...] = (0.25, 0.5, 1.0, 2.0),
) -> list[dict]:
    """Average fill price + realizable edge as the position scales.

    Re-simulates the fill at multiples of ``base_size_usd`` against live depth so
    the agent can see impact (price walking up the book) and where edge decays.
    """
    curve: list[dict] = []
    for k in multiples:
        size = round(base_size_usd * k, 2)
        if size <= 0:
            continue
        est = estimate_realizable_edge(legs, size, book_getter)
        curve.append({
            "size_usd": size,
            "avg_fill_price": est.avg_fill_price,
            "fillable_size_usd": est.fillable_size_usd,
            "realizable_edge": est.realizable_edge,
        })
    return curve


def execution_sizing(
    legs: list[Leg],
    size_usd: float,
    book_getter: BookGetter,
    bankroll_usd: float | None = None,
    fair_value: float | None = None,
) -> dict:
    """Kelly stake (if bankroll+fair value given) + the market-impact curve.

    ``fair_value`` is the agent's probability estimate for the position (e.g. the
    cross-venue consensus). Without it we can't size Kelly, but the impact curve
    is still returned since it needs only the book.
    """
    est = estimate_realizable_edge(legs, size_usd, book_getter)
    curve = impact_curve(legs, size_usd, book_getter)
    out: dict = {
        "impact_curve": curve,
        "kelly_fraction": None,
        "recommended_size_usd": None,
    }
    if fair_value is not None and est.avg_fill_price > 0:
        kf = kelly_fraction(fair_value, est.avg_fill_price)
        out["kelly_fraction"] = kf
        if bankroll_usd is not None and bankroll_usd > 0:
            out["recommended_size_usd"] = round(kf * bankroll_usd, 2)
    return out
