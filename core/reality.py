"""Reality feeds — the market vs the world (K7).

Every signal so far is intramarket: prices, books, cross-venue spreads, our own
resolution history. K7 adds an EXTERNAL anchor — a nowcast of the same event from
real-world data (a poll aggregator for politics, a macro nowcast for economics, an
on-chain / options read for crypto) — and measures the market's divergence from
it: "the market says 60%, the data says 45%." Where the toy market and the world
disagree, one of them is wrong — and that gap is a forecast edge no
prediction-market aggregator surfaces.

Structured exactly like the options cross-check (C7): the divergence math is a
pure function, fully testable offline; the live feed is a flag-gated provider that
degrades to None so nothing breaks without a feed configured. The operator points
``REALITY_NOWCAST_URL`` at whatever nowcast service they trust (a poll API, an
internal model); the reality-implied probability then feeds the house forecast
(K1) as an independent, heavily-weighted component and surfaces a divergence.

  REALITY_ENABLED      unset (default) | on
  REALITY_NOWCAST_URL  endpoint returning {"probability": p} for an entity query
  REALITY_SIGNAL_MIN   |divergence| that counts as actionable (default 0.10)
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod


def reality_enabled() -> bool:
    return os.getenv("REALITY_ENABLED", "").lower() in ("1", "on", "true", "yes")


def _clamp01(x: float) -> float:
    return min(1.0, max(0.0, x))


def divergence(market_prob: float, reality_prob: float) -> dict:
    """Signed gap between the market's probability and the real-world nowcast."""
    market_prob = _clamp01(market_prob)
    reality_prob = _clamp01(reality_prob)
    diff = round(market_prob - reality_prob, 4)
    threshold = float(os.getenv("REALITY_SIGNAL_MIN", "0.10"))
    return {
        "market_probability": round(market_prob, 4),
        "reality_probability": round(reality_prob, 4),
        "divergence": diff,  # + => market richer than the world says
        "direction": "market_rich" if diff > 0 else ("market_cheap" if diff < 0 else "aligned"),
        "signal": abs(diff) >= threshold,  # actionable disagreement
    }


def _entity_key(market) -> str:
    """A stable query key for a market — its parsed entity if available, else title."""
    try:
        from .entailment import parse_claim

        claim = parse_claim(market)
        if claim is not None:
            return claim.entity
    except Exception:
        pass
    return market.title


class RealityProvider(ABC):
    """Returns a real-world-implied probability for a market, or None."""

    @abstractmethod
    def implied_probability(self, market) -> float | None: ...


class HttpNowcastProvider(RealityProvider):
    """Generic nowcast feed: GET ``{url}?entity=…`` -> ``{"probability": p}``.

    Deliberately feed-agnostic so the operator can point it at any nowcast service
    (poll aggregator, macro model, internal). The HTTP client is injectable so the
    fetch path is testable without network; any failure returns None (degrade).
    """

    def __init__(self, url: str, client_factory=None):
        self.url = url
        self._client_factory = client_factory or self._default_client

    def _default_client(self):
        import httpx

        return httpx.Client(timeout=float(os.getenv("HTTP_TIMEOUT", "12")))

    def implied_probability(self, market) -> float | None:
        import httpx

        entity = _entity_key(market)
        try:
            with self._client_factory() as client:
                resp = client.get(self.url, params={"entity": entity})
            if resp.status_code != 200:
                return None
            data = resp.json()
        except (httpx.HTTPError, ValueError):
            return None
        p = data.get("probability") if isinstance(data, dict) else None
        if p is None:
            return None
        try:
            return _clamp01(float(p))
        except (TypeError, ValueError):
            return None


def get_provider() -> RealityProvider | None:
    """Configured reality provider, or None when disabled / no feed URL."""
    if not reality_enabled():
        return None
    url = os.getenv("REALITY_NOWCAST_URL")
    if not url:
        return None
    return HttpNowcastProvider(url)
