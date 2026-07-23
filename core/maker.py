"""Market-maker advisor — the other side of the book (K5).

Every tool so far serves a TAKER: find someone else's mispricing and lift it. K5
serves a MAKER: where to post two-sided limit quotes to *earn* the spread, and —
critically — when to step aside because the flow looks informed. It fuses signals
the server already computes: the book's current bid/ask, microstructure
(informed-flow / momentum, realized vol) and quote-quality flags, into a concrete
recommendation — a bid, an ask, how wide to quote, which way to skew, and a
``pull_quotes`` guard. Wider spreads and a lean away from informed flow are the
maker's defense against adverse selection (getting run over by someone who knows
more). Smaller, more sophisticated audience than takers — and a more valuable one.

Intelligence only: it recommends quotes, it never posts them. Pure/offline — the
caller supplies the already-computed book and microstructure signals.

  MAKER_MIN_HALF_SPREAD  floor on the half-spread you'd quote (default 0.01)
  MAKER_PULL_ADVERSE     adverse-pressure level at which to pull quotes (default 0.6)
  MAKER_SKEW_FACTOR      how hard to lean the center with informed flow (default 0.5)
"""

from __future__ import annotations

import os


def _clamp01(x: float) -> float:
    return min(1.0, max(0.0, x))


def advise(
    fair: float,
    best_bid: float | None = None,
    best_ask: float | None = None,
    momentum: float = 0.0,
    informed_flow: bool = False,
    realized_vol: float = 0.0,
    quality_flags: list[str] | None = None,
    fee: float = 0.0,
) -> dict:
    """Two-sided quote recommendation around ``fair`` under adverse-selection risk."""
    flags = quality_flags or []
    min_half = float(os.getenv("MAKER_MIN_HALF_SPREAD", "0.01"))
    pull_at = float(os.getenv("MAKER_PULL_ADVERSE", "0.6"))
    skew_factor = float(os.getenv("MAKER_SKEW_FACTOR", "0.5"))
    fair = _clamp01(fair)

    # --- adverse-selection pressure in [0,1]: how likely you're the sucker ---
    adverse = 0.0
    factors: list[str] = []
    if informed_flow:
        adverse += 0.4
        factors.append("informed flow on the tape")
    vol_add = min(0.3, realized_vol * 10.0)  # choppy tape -> quote wider
    if vol_add > 0:
        adverse += vol_add
    serious = [f for f in flags if f in ("crossed_book", "wide_spread", "stale")]
    if serious:
        adverse += min(0.2, 0.1 * len(serious))
        factors.append("low quote quality: " + ", ".join(serious))
    adverse = round(min(1.0, adverse), 4)

    # --- half-spread: current book, floored, widened by adverse pressure ---
    book_half = None
    if best_bid is not None and best_ask is not None and best_ask > best_bid:
        book_half = (best_ask - best_bid) / 2.0
    base_half = max(min_half, book_half if book_half is not None else min_half)
    half = round(base_half * (1.0 + adverse), 4)

    # --- skew: lean the center WITH informed flow so you don't sell too cheap
    #     into informed buying (or buy too dear into informed selling) ---
    skew = round(momentum * skew_factor * half, 4)
    center = _clamp01(fair + skew)
    bid = round(_clamp01(center - half), 4)
    ask = round(_clamp01(center + half), 4)

    pull = adverse >= pull_at
    capture = round((ask - bid) - 2.0 * fee, 4)  # gross spread capture per round trip
    return {
        "fair_value": round(fair, 4),
        "recommended_bid": bid,
        "recommended_ask": ask,
        "half_spread": half,
        "skew": skew,
        "adverse_pressure": adverse,
        "pull_quotes": pull,
        "expected_spread_capture": capture,
        "factors": factors,
        "note": "Post the bid/ask to earn the spread; the width and skew price "
                "adverse-selection risk. pull_quotes=true: step aside — the flow "
                "looks informed and you'd likely be picked off.",
    }
