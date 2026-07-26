"""Capital-velocity strategy — fast capital turnover (I1).

Every ranking so far optimizes edge *quality*; none prices TIME. For a
compounding bankroll the honest comparison is capital velocity: a 5% edge that
locks capital for a year compounds to 5%/yr, while a 1% edge recycled weekly
compounds to ~68%/yr. This module makes turnover a first-class strategy:

* **velocity metrics** per opportunity — ``capital_efficiency`` (expected value
  per day of locked capital, the ranking key), an illustrative
  ``compound_annual_growth`` under recycling, and **early-exit liquidity** (deep
  books on the exit side mean you can realize convergence without holding to
  resolution — the fastest turnover of all);
* a ``rank="velocity"`` mode for the flagship scanner;
* a **rotation plan**: allocate a bankroll across short-cycle opportunities and
  project the compounded bankroll over a horizon as capital recycles.

Honesty notes baked in: unknown holding periods use a conservative default and
are marked ``holding_known: false``; compound growth assumes similar
opportunities keep re-appearing (flagged in the plan's note) and is capped.

  VELOCITY_DEFAULT_DAYS    holding assumed when close time is unknown (default 30)
  ROTATION_MAX_POSITIONS   max concurrent positions in a rotation plan (default 5)
"""

from __future__ import annotations

import math
import os

from .models import Opportunity, Side

_GROWTH_CAP = 100.0  # 10,000%/yr display cap — beyond this the number is noise


def _default_days() -> float:
    return float(os.getenv("VELOCITY_DEFAULT_DAYS", "30"))


def _ev(opp: Opportunity) -> float:
    return opp.expected_value if opp.expected_value is not None else opp.realizable_edge


def _compound_growth(edge: float, cycles_per_year: float) -> float:
    """(1+edge)^cycles - 1, computed in log-space so a short holding + large edge
    can't OverflowError — the display cap is applied BEFORE exponentiating."""
    per = 1.0 + max(0.0, edge)
    if per <= 1.0:
        return 0.0
    log_final = cycles_per_year * math.log(per)
    if log_final >= math.log(_GROWTH_CAP + 1.0):  # would exceed the cap anyway
        return _GROWTH_CAP
    return round(math.exp(log_final) - 1.0, 4)


def _compound_factor(ev: float, cycles: float) -> float:
    """(1+ev)^cycles in log-space with the same cap as compound growth, so a
    short holding + large edge can't OverflowError the rotation plan."""
    per = 1.0 + max(0.0, ev)
    if per <= 1.0:
        return 1.0
    log_final = cycles * math.log(per)
    if log_final >= math.log(_GROWTH_CAP + 1.0):
        return _GROWTH_CAP + 1.0
    return math.exp(log_final)


def annotate_velocity(opps: list[Opportunity]) -> list[Opportunity]:
    """Attach the ``velocity`` block (holding, efficiency, compound growth) to
    every opportunity. Book-free and cheap — safe on the flagship's hot path.
    Early-exit liquidity (which needs orderbook fetches) is added separately by
    :func:`annotate_early_exit` only for the velocity-ranked shortlist."""
    for opp in opps:
        known = opp.holding_days is not None
        days = max(0.5, opp.holding_days if known else _default_days())
        efficiency = round(_ev(opp) / days, 6)  # EV per locked-capital day
        opp.velocity = {
            "holding_days": round(days, 2),
            "holding_known": known,
            "capital_efficiency": efficiency,
            "compound_annual_growth": _compound_growth(opp.realizable_edge, 365.0 / days),
            "velocity_score": efficiency,
        }
    return opps


def annotate_early_exit(opps: list[Opportunity], book_getter, limit: int = 10) -> list[Opportunity]:
    """Add early-exit liquidity to the top ``limit`` opportunities only.

    Kept off the default path because it fetches an orderbook per leg — doing it
    for every opportunity on every scan would add O(opps x legs) book fetches to
    live calls. Deep unwind-side depth means you can realize convergence without
    holding to resolution (the fastest turnover there is).
    """
    if book_getter is None:
        return opps
    for opp in opps[:limit]:
        if opp.velocity is None:
            continue
        exit_usd = _exit_liquidity(opp, book_getter)
        if exit_usd is not None:
            opp.velocity["early_exit_liquidity_usd"] = exit_usd
            opp.velocity["early_exit"] = exit_usd >= (opp.max_size_usd or 0.0)
    return opps


def _exit_liquidity(opp: Opportunity, book_getter) -> float | None:
    """USD depth available to UNWIND the position (min across legs), or None."""
    if book_getter is None or not opp.legs:
        return None
    depths: list[float] = []
    for leg in opp.legs:
        book = book_getter(leg.venue.value, leg.market_id)
        if not book:
            return None
        # Unwinding a YES sells into bids; unwinding a NO buys back from asks.
        levels = book.yes_bids if leg.side == Side.YES else book.yes_asks
        depths.append(sum(l.size_usd for l in levels))
    return round(min(depths), 2) if depths else None


def rank_by_velocity(opps: list[Opportunity]) -> list[Opportunity]:
    """Sort by capital velocity (EV per locked day), best turnover first."""
    opps.sort(key=lambda o: (o.velocity or {}).get("velocity_score", 0.0), reverse=True)
    return opps


def rotation_plan(
    opps: list[Opportunity],
    bankroll_usd: float,
    horizon_days: float = 30.0,
    max_positions: int | None = None,
) -> dict:
    """Allocate a bankroll across short-cycle opportunities and project the
    compounded bankroll over ``horizon_days`` as capital recycles.

    Greedy by capital efficiency; each pick is assumed to repeat with a similar
    profile when it resolves (cycles = horizon // holding). Transparent
    approximation, clearly flagged — a plan, not a promise.
    """
    if max_positions is None:
        max_positions = int(os.getenv("ROTATION_MAX_POSITIONS", "5"))
    candidates: list[tuple[Opportunity, float, float]] = []
    for opp in opps:
        v = opp.velocity or {}
        days = v.get("holding_days")
        ev = _ev(opp)
        if days is None or days > horizon_days or ev <= 0:
            continue
        candidates.append((opp, days, ev))
    candidates.sort(key=lambda t: t[2] / t[1], reverse=True)  # efficiency
    candidates = candidates[:max_positions]

    free = float(bankroll_usd)
    allocations: list[dict] = []
    projected = 0.0
    for opp, days, ev in candidates:
        if free <= 0:
            break
        alloc = round(min(opp.max_size_usd or 0.0, free), 2)
        if alloc <= 0:
            continue
        cycles = max(1, int(horizon_days // days))
        factor = _compound_factor(ev, cycles)
        allocations.append({
            "title": opp.title,
            "kind": opp.kind.value,
            "allocated_usd": alloc,
            "holding_days": round(days, 2),
            "cycles_in_horizon": cycles,
            "ev_per_cycle": round(ev, 4),
            "projected_usd": round(alloc * factor, 2),
            "legs": [{"venue": l.venue.value, "market_id": l.market_id,
                      "side": l.side.value} for l in opp.legs],
        })
        projected += alloc * factor
        free -= alloc
    projected += free  # unallocated capital carries through flat
    deployed = round(bankroll_usd - free, 2)
    return {
        "bankroll_usd": round(bankroll_usd, 2),
        "horizon_days": horizon_days,
        "deployed_usd": deployed,
        "idle_usd": round(free, 2),
        "positions": len(allocations),
        "allocations": allocations,
        "projected_bankroll_usd": round(projected, 2),
        "projected_return": round((projected - bankroll_usd) / bankroll_usd, 4)
        if bankroll_usd else 0.0,
        "note": "Assumes similar opportunities keep re-appearing each cycle and "
                "fills at recommended size — a capital-rotation plan, not a promise.",
    }
