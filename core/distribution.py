"""Implied distribution engine — the vol surface for prediction markets (H1).

A binary market gives you one point: P(BTC > 100k). But a *ladder* of threshold
markets on the same quantity — P(>100k), P(>150k), P(>200k) — traces the whole
survival function P(X > k), and from that you can reconstruct the market's entire
implied probability distribution: its CDF, its percentiles (p10 / median / p90),
its tail probabilities, an implied mean, and — the money shot — the implied
probability at ANY strike, whether or not a market exists there. That lets you
price a fair value for a strike the market hasn't listed, and arbitrage a listed
binary against the curve the rest of the ladder implies.

This is the prediction-market analogue of an options volatility surface, and no
prediction-market aggregator exposes it. Pure/offline; reuses the C1 claim parser
to read thresholds out of titles.
"""

from __future__ import annotations

from .entailment import parse_claim
from .models import Market


def _survival_points(markets: list[Market]) -> list[tuple[float, float]]:
    """(threshold, P(X > threshold)) points from 'above' threshold markets, with
    monotonicity enforced (survival must be non-increasing in the threshold)."""
    pts: dict[float, float] = {}
    for m in markets:
        claim = parse_claim(m)
        if claim is None or claim.direction != "above":
            continue
        pts[claim.threshold] = m.yes_price  # last wins if duplicated
    ordered = sorted(pts.items())
    # Enforce a non-increasing survival curve (a higher bar can't be more likely).
    out: list[tuple[float, float]] = []
    running = 1.0
    for k, s in ordered:
        s = min(s, running)
        out.append((k, s))
        running = s
    return out


def _interp_survival(points: list[tuple[float, float]], strike: float) -> float | None:
    """P(X > strike) by linear interpolation on the survival curve."""
    if len(points) < 2:
        return None
    if strike <= points[0][0]:
        return points[0][1]
    if strike >= points[-1][0]:
        return points[-1][1]
    for i in range(1, len(points)):
        k0, s0 = points[i - 1]
        k1, s1 = points[i]
        if k0 <= strike <= k1:
            if k1 == k0:
                return s0
            frac = (strike - k0) / (k1 - k0)
            return round(s0 + frac * (s1 - s0), 4)
    return None


def _percentile(points: list[tuple[float, float]], p: float) -> float | None:
    """Value k such that P(X <= k) = p — invert the CDF (= 1 - survival)."""
    target_survival = 1.0 - p
    if len(points) < 2:
        return None
    # Survival decreases with k; find where it crosses target.
    if target_survival >= points[0][1]:
        return points[0][0]
    if target_survival <= points[-1][1]:
        return points[-1][0]
    for i in range(1, len(points)):
        k0, s0 = points[i - 1]
        k1, s1 = points[i]
        if s1 <= target_survival <= s0:
            if s0 == s1:
                return k0
            frac = (s0 - target_survival) / (s0 - s1)
            return round(k0 + frac * (k1 - k0), 2)
    return None


def _implied_mean(points: list[tuple[float, float]]) -> float | None:
    """E[X] ≈ k0 + ∫ P(X>k) dk over the observed range (trapezoidal). A lower
    bound really — mass beyond the last threshold isn't observed — but a useful
    center-of-mass for a non-negative quantity."""
    if len(points) < 2:
        return None
    total = points[0][0]  # everything is at least the lowest threshold
    for i in range(1, len(points)):
        k0, s0 = points[i - 1]
        k1, s1 = points[i]
        total += (s0 + s1) / 2.0 * (k1 - k0)
    return round(total, 2)


def build_distribution(markets: list[Market]) -> dict | None:
    """Full implied distribution from a threshold ladder, or None if too sparse."""
    points = _survival_points(markets)
    if len(points) < 2:
        return None
    return {
        "survival_curve": [{"threshold": k, "prob_above": s} for k, s in points],
        "percentiles": {
            "p10": _percentile(points, 0.10),
            "p50_median": _percentile(points, 0.50),
            "p90": _percentile(points, 0.90),
        },
        "implied_mean": _implied_mean(points),
        "thresholds": len(points),
        "_points": points,  # internal, stripped by the resource layer
    }


def prob_above(markets: list[Market], strike: float) -> float | None:
    """Market-implied P(X > strike) at ANY strike (interpolated from the ladder)."""
    return _interp_survival(_survival_points(markets), strike)


def strike_arbitrage(markets: list[Market], strike: float, market_price: float,
                     cost_haircut: float = 0.01) -> dict | None:
    """Compare a listed binary at ``strike`` to what the rest of the ladder implies.

    If the listed price disagrees with the interpolated P(X>strike) by more than
    costs, the strike is mispriced relative to its own distribution.
    """
    implied = prob_above(markets, strike)
    if implied is None:
        return None
    gap = round(market_price - implied, 4)
    if abs(gap) - cost_haircut < 0:
        return None
    return {
        "strike": strike,
        "market_price": round(market_price, 4),
        "curve_implied": implied,
        "gap": gap,  # + => the listed market is rich vs its own distribution
        "edge": round(abs(gap) - cost_haircut, 4),
        "side": "no" if gap > 0 else "yes",
    }


def view(entity: str, markets: list[Market]) -> dict:
    """``distribution://{entity}`` payload — the implied distribution, minus
    internal state."""
    dist = build_distribution(markets)
    if dist is None:
        return {"entity": entity, "found": False,
                "note": "need >= 2 'X above K' threshold markets for the same event"}
    dist = dict(dist)
    dist.pop("_points", None)
    return {"entity": entity, "found": True, **dist}
