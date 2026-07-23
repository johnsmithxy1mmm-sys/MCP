"""Scenario engine — "what happens to everything if X?" (K2).

H2 gives pairwise P(A|B); J3 combines evidence about one target. K2 runs them
*forward*: pin one or more markets to a hypothetical outcome ("Fed cuts", "BTC
tops 150k") and propagate the consequences across the related market graph, so an
agent sees which markets move, by how much, and what that does to a portfolio.
It's the prediction-market analogue of a fund's scenario / stress test — a class
of tool no prediction-market product offers.

For each non-pinned target A, every pinned conditioner B contributes a conditional
P(A | B = assumed):
* B assumed YES → P(A | B) from the logical override (entailment ⇒ 1, exclusion ⇒
  0) if one exists, else the Gaussian copula (H2);
* B assumed NO  → the contrapositive of an entailment (A ⇒ B, so ¬B ⇒ ¬A ⇒ P=0),
  else copula conditioning on ¬B.
The per-target conditionals are fused into a posterior with the same naive-Bayes
log-odds engine as J3. ``shift = posterior − prior`` is the scenario's effect.

Pure/stdlib: correlations come from a supplied history getter; logic from the
event graph. Honest by construction — copula edges are estimates, flagged as such.
"""

from __future__ import annotations

from .conditional import conditional, _pearson
from .inference import infer_target
from .models import Side


def _conditional_prob(p_t: float, p_c: float, assume: float, rho: float,
                      target_id: str, cond_id: str, logical: dict) -> tuple[float, str]:
    """P(target | conditioner = assumed). Returns (probability, source)."""
    if assume >= 0.5:  # conditioner assumed YES
        override = logical.get((cond_id, target_id))  # exact P(target | cond=yes)
        if override is not None:
            return max(0.0, min(1.0, override)), "logical"
        return conditional(p_t, p_c, rho)["p_a_given_b"], "copula"
    # conditioner assumed NO
    entail = logical.get((target_id, cond_id))  # P(cond | target=yes)
    if entail is not None and entail >= 0.999:
        # target ⇒ cond, so ¬cond ⇒ ¬target.
        return 0.0, "logical"
    cond_d = conditional(p_t, p_c, rho)
    joint = cond_d["joint"]
    denom = 1.0 - p_c
    if denom <= 1e-9:  # conditioning on an ~impossible NO tells us nothing
        return round(p_t, 4), "prior"
    return max(0.0, min(1.0, round((p_t - joint) / denom, 4))), "copula"


def propagate(pins: dict[str, float], markets: list, history_getter,
              logical: dict | None = None) -> list[dict]:
    """Posterior shift for every non-pinned market under the pinned assumptions.

    ``pins`` maps market_id -> assumed YES probability (1.0 = YES, 0.0 = NO).
    ``markets`` is the related set; ``history_getter(venue, id) -> [PricePoint]``
    supplies the series for correlation. Returns shift rows sorted by |shift|.
    """
    logical = logical or {}
    by_id = {m.market_id: m for m in markets}
    series = {m.market_id: [p.yes_price for p in history_getter(m.venue.value, m.market_id)]
              for m in markets}

    shifts: list[dict] = []
    for target in markets:
        if target.market_id in pins:
            continue
        prior = max(0.0, min(1.0, target.yes_price))
        evidence: list[dict] = []
        drivers: list[dict] = []
        for cond_id, assume in pins.items():
            cond = by_id.get(cond_id)
            if cond is None:
                continue
            rho = _pearson(series.get(target.market_id, []), series.get(cond_id, []))
            p, source = _conditional_prob(
                prior, cond.yes_price, assume, rho, target.market_id, cond_id, logical)
            evidence.append({"conditional": p, "label": cond_id})
            drivers.append({"conditioner": cond_id,
                            "assumed": "yes" if assume >= 0.5 else "no",
                            "p_target_given_assumption": round(p, 4), "source": source})
        if not evidence:
            continue
        inferred = infer_target(prior, evidence)
        shifts.append({
            "market_id": target.market_id,
            "venue": target.venue.value,
            "title": target.title,
            "prior": inferred["prior"],
            "posterior": inferred["posterior"],
            "shift": inferred["shift"],
            "drivers": drivers,
        })
    shifts.sort(key=lambda s: abs(s["shift"]), reverse=True)
    return shifts


def portfolio_impact(shifts: list[dict], legs: list) -> dict:
    """Net effect of a scenario on a portfolio of legs.

    A YES leg gains as its market's YES probability rises (+shift); a NO leg gains
    as it falls (−shift). Returns the per-leg probability impact and the net — a
    stress test of the book under the assumed scenario.
    """
    shift_by_id = {s["market_id"]: s["shift"] for s in shifts}
    per_leg: list[dict] = []
    net = 0.0
    for leg in legs:
        shift = shift_by_id.get(leg.market_id)
        if shift is None:
            continue
        signed = shift if leg.side == Side.YES else -shift
        net += signed
        per_leg.append({
            "venue": leg.venue.value, "market_id": leg.market_id,
            "side": leg.side.value, "value_impact": round(signed, 4),
        })
    return {
        "net_value_impact": round(net, 4),  # sum of per-position probability moves
        "affected_legs": len(per_leg),
        "legs": per_leg,
        "note": "Impact is the scenario-driven change in each position's YES-equivalent "
                "probability; sum it against your sizes for a dollar stress figure.",
    }
