"""Quote-quality guard — don't flag edges on garbage prices (F7).

The track record is hurt less by our math than by bad venue quotes: a stale
snapshot, a crossed book, a wash-traded print. Scoring quote quality lets the
scanners skip opportunities built on unreliable prices — fewer false flags means
a better hit-rate, which is the server's single strongest marketing asset.

Pure/offline. A gate is applied only when ``QUALITY_GATE`` is set (default off),
so the mock/offline behavior is unchanged; the score is always available for
callers that want to surface it.

  QUALITY_GATE            unset (default) | on  — filter low-quality quotes in scans
  QUALITY_MIN             minimum usable score (default 0.5)
  QUALITY_STALE_SECONDS   age past which a quote is 'stale' (default 300)
  QUALITY_THIN_USD        volume below which a market is 'thin' (default 5000)
  QUALITY_WIDE_SPREAD     spread above which a book is 'wide' (default 0.10)
"""

from __future__ import annotations

import os


def quality_min() -> float:
    return float(os.getenv("QUALITY_MIN", "0.5"))


def gate_enabled() -> bool:
    return os.getenv("QUALITY_GATE", "").lower() in ("1", "on", "true", "yes")


def quote_quality(yes_price, volume_usd, data_age_seconds=None, orderbook=None) -> dict:
    """Score a quote in [0, 1] with human-readable flags. Higher = more reliable."""
    flags: list[str] = []
    score = 1.0

    stale_after = float(os.getenv("QUALITY_STALE_SECONDS", "300"))
    if data_age_seconds is not None and data_age_seconds > stale_after:
        flags.append("stale")
        score -= 0.3

    thin = float(os.getenv("QUALITY_THIN_USD", "5000"))
    if not volume_usd or volume_usd < thin:
        flags.append("thin")
        score -= 0.2

    # Degenerate price with real volume is less informative than it looks.
    if yes_price is not None and (yes_price <= 0.01 or yes_price >= 0.99):
        flags.append("extreme_price")
        score -= 0.1

    if orderbook is not None:
        tb, ta = orderbook.top_bid_price, orderbook.top_ask_price
        if tb is not None and ta is not None:
            if tb >= ta:
                flags.append("crossed_book")  # bid >= ask: broken/stale book
                score -= 0.4
            elif (ta - tb) > float(os.getenv("QUALITY_WIDE_SPREAD", "0.10")):
                flags.append("wide_spread")
                score -= 0.2

    score = max(0.0, round(score, 3))
    return {"score": score, "flags": flags, "usable": score >= quality_min()}


def is_usable(market, orderbook=None, data_age_seconds=None) -> bool:
    """Whether a market's quote passes the quality gate. Always True when the gate
    is off (default) — quality is advisory unless QUALITY_GATE is set."""
    if not gate_enabled():
        return True
    q = quote_quality(getattr(market, "yes_price", None),
                      getattr(market, "volume_usd", None), data_age_seconds, orderbook)
    return q["usable"]
