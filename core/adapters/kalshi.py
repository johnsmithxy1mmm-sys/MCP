"""Kalshi adapter.

Data source: https://api.elections.kalshi.com/trade-api/v2
* GET /markets                      — market metadata + best bid/ask (in cents)
* GET /markets/{ticker}/orderbook   — depth (price in cents, size in contracts)

Kalshi prices are integer cents (0..100); we normalize to 0..1. Read endpoints
are largely public; if KALSHI_API_KEY is set it's sent as a bearer token.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

from ..models import Market, OrderbookLevel, OrderbookSnapshot, Venue
from .base import VenueAdapter, _matches_query, fetch_json

KALSHI_URL = os.getenv("KALSHI_API_URL", "https://api.elections.kalshi.com/trade-api/v2")


def _cents_to_prob(cents) -> float:
    try:
        return round(float(cents) / 100.0, 4)
    except (TypeError, ValueError):
        return 0.0


def _auth_headers() -> dict:
    key = os.getenv("KALSHI_API_KEY")
    return {"Authorization": f"Bearer {key}"} if key else {}


class KalshiAdapter(VenueAdapter):
    venue = Venue.KALSHI

    def fetch_markets(self, query: str | None = None, limit: int = 50) -> list[Market]:
        data = fetch_json(
            KALSHI_URL, "/markets",
            params={"limit": limit, "status": "open"},
            headers=_auth_headers(), venue="Kalshi",
        )
        rows = (data or {}).get("markets", [])
        markets: list[Market] = []
        for row in rows:
            m = self._normalize_market(row)
            if m is None:
                continue
            if not _matches_query(query, m):
                continue
            markets.append(m)
        return markets

    def _normalize_market(self, row: dict) -> Market | None:
        ticker = str(row.get("ticker") or "").strip()
        if not ticker:
            return None
        # Prefer midpoint of best bid/ask; fall back to last_price.
        yes_bid, yes_ask = row.get("yes_bid"), row.get("yes_ask")
        if yes_bid is not None and yes_ask is not None:
            yes = round((_cents_to_prob(yes_bid) + _cents_to_prob(yes_ask)) / 2, 4)
        else:
            yes = _cents_to_prob(row.get("last_price"))
        yes = min(1.0, max(0.0, yes))
        return Market(
            venue=Venue.KALSHI,
            market_id=ticker,
            title=str(row.get("title") or row.get("subtitle") or ticker).strip(),
            category=(str(row["category"]).lower() if row.get("category") else None),
            yes_price=yes,
            no_price=round(1.0 - yes, 4),
            volume_usd=float(row.get("volume") or 0) or None,
        )

    def fetch_orderbook(self, market_id: str) -> OrderbookSnapshot | None:
        data = fetch_json(
            KALSHI_URL, f"/markets/{market_id}/orderbook",
            headers=_auth_headers(), venue="Kalshi",
        )
        book = (data or {}).get("orderbook", {}) or {}
        # Kalshi returns YES bids under "yes" and NO bids under "no"; a YES ask is
        # the complement of a NO bid: price_yes_ask = 1 - price_no_bid.
        yes_levels = book.get("yes") or []
        no_levels = book.get("no") or []
        bids = [
            OrderbookLevel(price=_cents_to_prob(p), size_usd=round(float(s), 2))
            for p, s in yes_levels
        ]
        asks = [
            OrderbookLevel(price=round(1.0 - _cents_to_prob(p), 4), size_usd=round(float(s), 2))
            for p, s in no_levels
        ]
        bids.sort(key=lambda l: l.price, reverse=True)
        asks.sort(key=lambda l: l.price)
        return OrderbookSnapshot(
            venue=Venue.KALSHI,
            market_id=market_id,
            as_of=datetime.now(timezone.utc),
            yes_asks=asks[:10],
            yes_bids=bids[:10],
        )
