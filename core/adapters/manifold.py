"""Manifold Markets adapter.

Data source (public, no auth):
* REST  https://api.manifold.markets/v0/markets       — market list
        https://api.manifold.markets/v0/market/{id}   — single market detail

Manifold binary markets expose a live ``probability`` (0..1) directly, so YES
price maps straight to it. Manifold is a CPMM (no central limit order book), so
we synthesize a shallow two-level book from the probability and the market's
liquidity — enough for the realizable-edge model to reason about size, clearly
derived from pool depth rather than resting orders.

A third venue matters for the fair-value engine: two venues give a spread, three
give a consensus with a real dispersion signal (B3/B8).
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

from ..models import Market, OrderbookLevel, OrderbookSnapshot, Venue
from .base import VenueAdapter, _matches_query, fetch_json

MANIFOLD_URL = "https://api.manifold.markets"


def _f(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class ManifoldAdapter(VenueAdapter):
    venue = Venue.MANIFOLD

    def __init__(self, base_url: str | None = None):
        self.base_url = base_url or os.getenv("MANIFOLD_API_URL", MANIFOLD_URL)
        self._liquidity: dict[str, float] = {}

    # -- markets ----------------------------------------------------------
    def fetch_markets(self, query: str | None = None, limit: int = 50) -> list[Market]:
        rows = fetch_json(self.base_url, "/v0/markets", params={"limit": limit}, venue="Manifold")
        markets: list[Market] = []
        for row in rows if isinstance(rows, list) else []:
            m = self._normalize_market(row)
            if m is None or not _matches_query(query, m):
                continue
            markets.append(m)
        return markets

    def _normalize_market(self, row: dict) -> Market | None:
        if row.get("outcomeType") not in (None, "BINARY"):
            return None  # only binary markets normalize to a single YES price
        if row.get("isResolved"):
            return None
        market_id = str(row.get("id") or "").strip()
        prob = row.get("probability")
        if not market_id or prob is None:
            return None
        yes = min(1.0, max(0.0, _f(prob)))
        liquidity = _f(row.get("totalLiquidity") or row.get("pool", {}).get("YES"))
        self._liquidity[market_id] = liquidity
        close_time = None
        close_ms = row.get("closeTime")
        if close_ms:
            try:
                close_time = datetime.fromtimestamp(_f(close_ms) / 1000.0, tz=timezone.utc)
            except (ValueError, OSError):
                close_time = None
        return Market(
            venue=Venue.MANIFOLD,
            market_id=market_id,
            title=str(row.get("question") or market_id).strip(),
            category=(str(row["category"]).lower() if row.get("category") else None),
            yes_price=round(yes, 4),
            no_price=round(1.0 - yes, 4),
            volume_usd=_f(row.get("volume")) or None,
            close_time=close_time,
        )

    # -- orderbook (synthesized from CPMM state) --------------------------
    def fetch_orderbook(self, market_id: str) -> OrderbookSnapshot | None:
        row = fetch_json(self.base_url, f"/v0/market/{market_id}", venue="Manifold")
        if not isinstance(row, dict) or row.get("probability") is None:
            return None
        yes = min(1.0, max(0.0, _f(row.get("probability"))))
        liquidity = _f(row.get("totalLiquidity")) or self._liquidity.get(market_id, 0.0)
        # CPMM has no resting book; approximate depth as a fraction of liquidity at
        # the quoted price, with a second, worse level to model slippage.
        depth = round(max(0.0, liquidity) * 0.25, 2)
        spread = 0.01
        bid_price = round(max(0.0, yes - spread), 4)
        ask_price = round(min(1.0, yes + spread), 4)
        return OrderbookSnapshot(
            venue=Venue.MANIFOLD,
            market_id=market_id,
            as_of=datetime.now(timezone.utc),
            yes_asks=[
                OrderbookLevel(price=ask_price, size_usd=depth),
                OrderbookLevel(price=round(min(1.0, ask_price + spread), 4), size_usd=depth * 2),
            ],
            yes_bids=[
                OrderbookLevel(price=bid_price, size_usd=depth),
                OrderbookLevel(price=round(max(0.0, bid_price - spread), 4), size_usd=depth * 2),
            ],
        )
