"""Fair-value / consensus-probability engine (B3).

Two venues quoting the same event give you more than a pairwise spread: their
liquidity-weighted consensus is an estimate of the market's *fair* probability,
and each venue's deviation from it tells you which side is rich or cheap and by
how much. This anchors ``compare_across_venues`` (report the consensus, not just
the gap) and ``find_mispricing`` (edge relative to fair value, with a direction).

Pure and offline — operates on canonical :class:`Market` objects only.

  FAIRVALUE_MIN_LIQUIDITY_USD  floor so a near-zero-volume quote can't dominate
                               the weighting (default 1.0)
"""

from __future__ import annotations

import os

from .models import Market


def _liquidity_weight(m: Market) -> float:
    """Weight a venue's quote by its liquidity (more volume = more informative)."""
    floor = float(os.getenv("FAIRVALUE_MIN_LIQUIDITY_USD", "1.0"))
    return max(floor, float(m.volume_usd or 0.0))


def consensus(markets: list[Market]) -> float | None:
    """Liquidity-weighted consensus YES probability across quoting venues.

    Returns None if there's nothing to average. A deeper venue pulls the fair
    value toward its price because its quote reflects more capital at risk.
    """
    if not markets:
        return None
    weights = [_liquidity_weight(m) for m in markets]
    total = sum(weights)
    if total <= 0:
        return round(sum(m.yes_price for m in markets) / len(markets), 4)
    return round(sum(m.yes_price * w for m, w in zip(markets, weights)) / total, 4)


def assess(markets: list[Market]) -> dict:
    """Fair value + per-venue deviation + a dispersion-based confidence.

    ``confidence`` is high when venues agree (tight dispersion) and low when they
    disagree — a wide spread means the consensus is a weaker anchor.
    """
    fair = consensus(markets)
    if fair is None:
        return {"fair_value": None, "confidence": 0.0, "venues": []}
    venues = [
        {
            "venue": m.venue.value,
            "market_id": m.market_id,
            "yes_price": m.yes_price,
            "deviation": round(m.yes_price - fair, 4),  # +rich / -cheap vs fair
            "liquidity_usd": m.volume_usd,
        }
        for m in markets
    ]
    dispersion = max((abs(v["deviation"]) for v in venues), default=0.0)
    confidence = round(max(0.0, 1.0 - dispersion * 2.0), 3)  # 0 spread -> 1.0
    return {"fair_value": fair, "confidence": confidence, "venues": venues}


def mispriced_venue(markets: list[Market], min_deviation: float) -> dict | None:
    """The single venue deviating most from fair value, if beyond a threshold.

    Direction: a venue trading BELOW fair value is a YES buy (underpriced), one
    ABOVE is a NO buy (overpriced). Returns None when everyone hugs consensus.
    """
    a = assess(markets)
    if a["fair_value"] is None or not a["venues"]:
        return None
    worst = max(a["venues"], key=lambda v: abs(v["deviation"]))
    if abs(worst["deviation"]) < min_deviation:
        return None
    return {
        "fair_value": a["fair_value"],
        "confidence": a["confidence"],
        "venue": worst["venue"],
        "market_id": worst["market_id"],
        "yes_price": worst["yes_price"],
        "deviation": worst["deviation"],
        "side": "no" if worst["deviation"] > 0 else "yes",
        "edge": round(abs(worst["deviation"]), 4),
    }
