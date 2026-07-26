"""Options-implied probability — real-money cross-check (C8/№4).

Play-money and small prediction markets say P(BTC > 100k). Billions in Deribit
options say it too — via the risk-neutral density implied by the option chain.
Comparing the two is a signal no prediction-market aggregator has: where the toy
market and the real-money market disagree, one of them is wrong, and the options
market is usually right.

The math is Breeden–Litzenberger: for European calls C(K), the risk-neutral
CDF is P(S ≤ K) = 1 + e^{rT}·dC/dK, so **P(S > K) = -e^{rT}·dC/dK** — the negative
slope of the call price across strikes. That's a pure function of an option chain
(:func:`prob_above`), fully testable offline. The live chain fetch (:class:`DeribitProvider`)
is flag-gated and degrades to None so nothing breaks without network/keys.

  OPTIONS_ENABLED   unset (default) | on
  DERIBIT_API_URL   https://www.deribit.com (override for tests/self-host)
"""

from __future__ import annotations

import math
import os

DERIBIT_URL = os.getenv("DERIBIT_API_URL", "https://www.deribit.com")


def options_enabled() -> bool:
    return os.getenv("OPTIONS_ENABLED", "").lower() in ("1", "on", "true", "yes")


def prob_above(chain: list[tuple[float, float]], strike: float, rate: float = 0.0, t_years: float = 0.0) -> float | None:
    """Risk-neutral P(S > strike) from a call chain via Breeden–Litzenberger.

    ``chain`` is ``[(strike, call_price), …]`` in a common currency. Uses the
    local negative slope of C(K) (a central difference where possible), which
    equals the risk-neutral exceedance probability. Returns None if the chain is
    too sparse to bracket the strike.
    """
    pts = sorted((k, c) for k, c in chain if k > 0 and c >= 0)
    if len(pts) < 2:
        return None
    discount = math.exp(rate * t_years)
    # Nearest node to the strike; use a CENTRAL difference across its neighbors
    # (falls back to the end segment at the boundaries) — the local dC/dK.
    j = min(range(len(pts)), key=lambda i: abs(pts[i][0] - strike))
    if 0 < j < len(pts) - 1:
        (k0, c0), (k1, c1) = pts[j - 1], pts[j + 1]
    elif j == 0:
        (k0, c0), (k1, c1) = pts[0], pts[1]
    else:
        (k0, c0), (k1, c1) = pts[-2], pts[-1]
    if k1 == k0:
        return None
    slope = (c1 - c0) / (k1 - k0)  # dC/dK, negative for calls
    prob = -discount * slope
    return round(min(1.0, max(0.0, prob)), 4)


def divergence(market_prob: float, options_prob: float) -> dict:
    """Signed gap between a prediction-market prob and the options-implied prob."""
    diff = round(market_prob - options_prob, 4)
    return {
        "market_probability": round(market_prob, 4),
        "options_probability": round(options_prob, 4),
        "divergence": diff,  # + => market richer than real-money options
        "direction": "market_rich" if diff > 0 else ("market_cheap" if diff < 0 else "aligned"),
    }


class DeribitProvider:
    """Fetches a Deribit call chain and computes options-implied probabilities.

    Flag-gated + network. The HTTP client is injectable so the chain-fetch path
    is testable without hitting Deribit. Any failure returns None (degrade).
    """

    def __init__(self, base_url: str | None = None, client_factory=None):
        self.base_url = (base_url or DERIBIT_URL).rstrip("/")
        self._client_factory = client_factory or self._default_client

    def _default_client(self):
        import httpx

        return httpx.Client(base_url=self.base_url, timeout=float(os.getenv("HTTP_TIMEOUT", "12")))

    def _get(self, path: str, params: dict):
        import httpx

        try:
            with self._client_factory() as client:
                resp = client.get(path, params=params)
            if resp.status_code != 200:
                return None
            return resp.json()
        except (httpx.HTTPError, ValueError):
            return None

    def call_chain(self, currency: str) -> list[tuple[float, float]]:
        """USD call prices per strike from Deribit's book summary."""
        data = self._get(
            "/api/v2/public/get_book_summary_by_currency",
            {"currency": currency.upper(), "kind": "option"},
        )
        if not data or "result" not in data:
            return []
        chain: list[tuple[float, float]] = []
        for row in data["result"]:
            name = row.get("instrument_name", "")
            if not name.endswith("-C"):  # calls only
                continue
            parts = name.split("-")
            if len(parts) < 4:
                continue
            try:
                strike = float(parts[2])
            except ValueError:
                continue
            mark = row.get("mark_price")
            underlying = row.get("underlying_price") or row.get("estimated_delivery_price")
            if mark is None or underlying is None:
                continue
            chain.append((strike, float(mark) * float(underlying)))  # BTC-terms -> USD
        return chain

    def implied_probability(self, currency: str, strike: float) -> float | None:
        chain = self.call_chain(currency)
        return prob_above(chain, strike)


def get_provider() -> DeribitProvider | None:
    """Configured Deribit provider, or None when OPTIONS_ENABLED is unset."""
    if not options_enabled():
        return None
    return DeribitProvider()
