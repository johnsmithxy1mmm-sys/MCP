"""Portfolio advisor — from selling signals to selling risk management (F4).

Every other tool judges one opportunity. An agent running a book has a different
question: *given everything I already hold, what's my real exposure and how do I
size and hedge it?* That's a second product built almost entirely from parts we
already have — the event graph (entity grouping), the price history
(co-movement), fractional Kelly (sizing), and the options module (delta hedges).

This computes:
* **net exposure by entity** — many legs can be one concentrated bet;
* **cross-market correlation** from our own recorded history — two "different"
  positions that move together aren't diversified;
* **correlation-adjusted portfolio Kelly** — one combined stake, shrunk for
  correlated legs, instead of naive per-leg sizing;
* **concrete hedges** — opposing markets on other venues for each net exposure.

Pure/offline over injected getters, so it works for mock and live alike.
"""

from __future__ import annotations

from .eventgraph import entity_of
from .models import Leg, Side


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    n = min(len(xs), len(ys))
    if n < 3:
        return None
    xs, ys = xs[-n:], ys[-n:]
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    if vx <= 0 or vy <= 0:
        return None
    return round(cov / (vx ** 0.5 * vy ** 0.5), 4)


def _series(history_getter, venue, market_id, window=50) -> list[float]:
    pts = history_getter(venue, market_id)
    return [p.yes_price for p in pts][-window:]


def assess(legs, market_getter, history_getter, related_getter,
           bankroll=None, fair_values=None) -> dict:
    """Portfolio-level exposure, correlation, sizing and hedges for ``legs``."""
    positions = []
    for leg in legs:
        m = market_getter(leg.venue.value, leg.market_id)
        if m is None:
            continue
        entity = entity_of(m) or (m.title or "").lower()
        positions.append({"leg": leg, "market": m, "entity": entity})
    if not positions:
        return {"error": "no_resolvable_legs"}

    exposure = _net_exposure(positions)
    correlations = _correlations(positions, history_getter)
    sizing = _portfolio_kelly(positions, correlations, bankroll, fair_values)
    hedges = _hedges(exposure, positions, related_getter)
    return {
        "positions": len(positions),
        "net_exposure": exposure,
        "concentration": _concentration(exposure),
        "correlations": correlations,
        "sizing": sizing,
        "hedges": hedges,
    }


def _net_exposure(positions) -> list[dict]:
    """Signed unit exposure per entity (YES = +1, NO = -1)."""
    by_entity: dict[str, dict] = {}
    for p in positions:
        e = by_entity.setdefault(p["entity"], {"entity": p["entity"], "net": 0, "long": 0,
                                               "short": 0, "market_ids": []})
        if p["leg"].side == Side.YES:
            e["net"] += 1; e["long"] += 1
        else:
            e["net"] -= 1; e["short"] += 1
        e["market_ids"].append(p["leg"].market_id)
    return sorted(by_entity.values(), key=lambda e: abs(e["net"]), reverse=True)


def _concentration(exposure) -> dict:
    """How lopsided the book is: the largest single-entity gross exposure share."""
    gross = sum(abs(e["net"]) for e in exposure)
    if gross == 0:
        return {"top_entity": None, "share": 0.0, "warning": None}
    top = max(exposure, key=lambda e: abs(e["net"]))
    share = round(abs(top["net"]) / gross, 4)
    warning = ("concentrated: most exposure is a single event"
               if share >= 0.6 and len(exposure) > 1 else None)
    return {"top_entity": top["entity"], "share": share, "warning": warning}


def _correlations(positions, history_getter) -> list[dict]:
    out = []
    for i in range(len(positions)):
        for j in range(i + 1, len(positions)):
            a, b = positions[i], positions[j]
            sa = _series(history_getter, a["leg"].venue.value, a["leg"].market_id)
            sb = _series(history_getter, b["leg"].venue.value, b["leg"].market_id)
            corr = _pearson(sa, sb)
            if corr is not None:
                out.append({"a": a["leg"].market_id, "b": b["leg"].market_id,
                            "correlation": corr})
    return out


def _portfolio_kelly(positions, correlations, bankroll, fair_values) -> dict:
    """Correlation-shrunk combined Kelly. Needs fair_values to size; else None."""
    if not fair_values:
        return {"note": "pass fair_values (your probability per market_id) for Kelly sizing",
                "combined_fraction": None}
    from .sizing import kelly_fraction

    per_leg = []
    for p in positions:
        fv = fair_values.get(p["leg"].market_id)
        if fv is None:
            continue
        m = p["market"]
        price = m.yes_price if p["leg"].side == Side.YES else round(1 - m.yes_price, 4)
        prob = fv if p["leg"].side == Side.YES else round(1 - fv, 4)
        kf = kelly_fraction(prob, price)
        per_leg.append({"market_id": p["leg"].market_id, "kelly_fraction": kf})
    if not per_leg:
        return {"combined_fraction": None}
    naive = sum(x["kelly_fraction"] for x in per_leg)
    # Shrink for average positive correlation: correlated bets aren't independent.
    avg_corr = (sum(max(0.0, c["correlation"]) for c in correlations) / len(correlations)
                if correlations else 0.0)
    combined = round(min(1.0, naive * (1.0 - avg_corr)), 4)
    out = {"per_leg": per_leg, "naive_fraction": round(naive, 4),
           "avg_correlation": round(avg_corr, 4), "combined_fraction": combined}
    if bankroll:
        out["recommended_size_usd"] = round(combined * bankroll, 2)
    return out


def _hedges(exposure, positions, related_getter) -> list[dict]:
    """For each net exposure, offsetting markets on other venues."""
    held = {p["leg"].market_id for p in positions}
    out = []
    for e in exposure:
        if e["net"] == 0:
            continue
        candidates = []
        for m in related_getter(e["entity"]) or []:
            if m.market_id in held:
                continue
            candidates.append({"venue": m.venue.value, "market_id": m.market_id,
                               "title": m.title, "yes_price": m.yes_price})
        if candidates:
            out.append({
                "entity": e["entity"],
                "direction": "long" if e["net"] > 0 else "short",
                "hedge_by": "sell YES / buy NO" if e["net"] > 0 else "buy YES",
                "candidates": candidates[:5],
            })
    return out
