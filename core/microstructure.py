"""Price microstructure / informed-flow proxy (C8).

Edge shows up in the *tape* before it shows up in a spread: a market being walked
in one direction on rising volatility is often informed flow arriving ahead of a
move. This computes a lightweight microstructure read from the recent price
series — tick-direction imbalance, realized volatility, and short-term drift —
into a signed momentum score and an ``informed_flow`` flag. It's the difference
between "there's an edge now" (every scanner) and "an edge is *about to* appear"
(a far better trigger for a watch).

Pure/offline: operates on a ``PricePoint`` series, so it works from the recorded
history in mock mode and from the live WSS stream in production alike.

  MICROSTRUCTURE_WINDOW  points in the lookback (default 20)
"""

from __future__ import annotations

import os

from .models import PricePoint


def compute(points: list[PricePoint], window: int | None = None) -> dict | None:
    """Microstructure read over the last ``window`` price points, or None if the
    series is too short to be meaningful."""
    w = window if window is not None else int(os.getenv("MICROSTRUCTURE_WINDOW", "20"))
    prices = [p.yes_price for p in points][-w:]
    if len(prices) < 3:
        return None
    diffs = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
    ups = sum(1 for d in diffs if d > 0)
    downs = sum(1 for d in diffs if d < 0)
    moves = ups + downs
    uptick_ratio = round(ups / moves, 4) if moves else 0.5
    drift = round(prices[-1] - prices[0], 4)

    mean = sum(diffs) / len(diffs)
    var = sum((d - mean) ** 2 for d in diffs) / len(diffs)
    realized_vol = round(var ** 0.5, 6)

    # Signed momentum in [-1, 1]: how one-sided the tape is.
    momentum = round((uptick_ratio - 0.5) * 2.0, 4)
    # Informed-flow proxy: a strongly one-sided tape that's actually moving price.
    informed_flow = abs(momentum) >= 0.5 and abs(drift) >= max(realized_vol, 1e-6)

    return {
        "uptick_ratio": uptick_ratio,
        "drift": drift,
        "realized_vol": realized_vol,
        "momentum": momentum,          # +up / -down, magnitude = one-sidedness
        "informed_flow": informed_flow,
        "direction": "up" if drift > 0 else ("down" if drift < 0 else "flat"),
        "samples": len(prices),
    }
