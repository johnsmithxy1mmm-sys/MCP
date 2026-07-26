"""Polymarket adapter.

Data sources (public, no auth):
* Gamma API  https://gamma-api.polymarket.com/markets  — market metadata + prices
* CLOB API   https://clob.polymarket.com/book?token_id=…  — orderbook depth

Prices are already normalized 0..1. Orderbook sizes are in shares (~$1 notional
at settlement), which we treat as USD-ish depth for the edge model.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from ..models import Market, OrderbookLevel, OrderbookSnapshot, Venue
from .base import VenueAdapter, fetch_json, normalize_rows

GAMMA_URL = os.getenv("POLYMARKET_GAMMA_URL", "https://gamma-api.polymarket.com")
CLOB_URL = os.getenv("POLYMARKET_CLOB_URL", "https://clob.polymarket.com")


def _as_list(value):
    """Gamma returns some array fields as JSON-encoded strings."""
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return []
    return []


def _f(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class PolymarketAdapter(VenueAdapter):
    venue = Venue.POLYMARKET

    def __init__(self):
        # conditionId -> YES clob token id, populated during fetch_markets.
        self._token_by_market: dict[str, str] = {}

    # -- markets ----------------------------------------------------------
    def fetch_markets(self, query: str | None = None, limit: int = 50) -> list[Market]:
        params = {
            "limit": limit, "closed": "false", "active": "true",
            "order": "volume", "ascending": "false",
        }
        rows = fetch_json(GAMMA_URL, "/markets", params=params, venue="Polymarket")
        return normalize_rows(rows, self._normalize_market, query)

    def _normalize_market(self, row: dict) -> Market | None:
        prices = _as_list(row.get("outcomePrices"))
        outcomes = _as_list(row.get("outcomes"))
        if len(prices) < 2:
            return None  # skip non-binary / malformed
        # Find the YES index (default 0).
        yes_idx = 0
        for i, o in enumerate(outcomes):
            if str(o).strip().lower() == "yes":
                yes_idx = i
                break
        no_idx = 1 - yes_idx if len(prices) == 2 else (yes_idx + 1) % len(prices)
        market_id = str(row.get("conditionId") or row.get("id") or "").strip()
        if not market_id:
            return None
        tokens = _as_list(row.get("clobTokenIds"))
        if tokens:
            self._token_by_market[market_id] = str(tokens[yes_idx] if yes_idx < len(tokens) else tokens[0])
        close_time = None
        if row.get("endDate"):
            try:
                close_time = datetime.fromisoformat(str(row["endDate"]).replace("Z", "+00:00"))
            except ValueError:
                close_time = None
        # negRisk markets are one mutually-exclusive event split into outcomes;
        # the shared id groups them into a multi-outcome market.
        event_group = str(row.get("negRiskMarketID") or "").strip() or None
        return Market(
            venue=Venue.POLYMARKET,
            market_id=market_id,
            title=str(row.get("question") or row.get("title") or "").strip(),
            category=(str(row["category"]).lower() if row.get("category") else None),
            yes_price=round(_f(prices[yes_idx]), 4),
            no_price=round(_f(prices[no_idx]), 4),
            volume_usd=_f(row.get("volume") or row.get("volumeNum")),
            close_time=close_time,
            event_group=event_group,
        )

    def _resolve_token(self, market_id: str) -> str | None:
        """YES clob token id for a conditionId — cache first, then Gamma lookup.

        Never silently sends a conditionId as a token_id (that returns an empty
        book). Returns None if the market can't be resolved to a token.
        """
        token = self._token_by_market.get(market_id)
        if token:
            return token
        # Gamma fallback: fetch this one market and extract its clobTokenIds.
        try:
            rows = fetch_json(
                GAMMA_URL, "/markets",
                params={"condition_ids": market_id, "limit": 1},
                venue="Polymarket",
            )
        except Exception:
            return None
        for row in rows if isinstance(rows, list) else []:
            self._normalize_market(row)  # populates self._token_by_market
        return self._token_by_market.get(market_id)

    def fetch_resolution(self, market_id: str) -> int | None:
        """Settled outcome from Gamma: a closed market whose YES price has snapped
        to ~1 (YES) or ~0 (NO). None while still trading."""
        try:
            rows = fetch_json(GAMMA_URL, "/markets",
                              params={"condition_ids": market_id, "limit": 1},
                              venue="Polymarket")
        except Exception:
            return None
        for row in rows if isinstance(rows, list) else []:
            if not (row.get("closed") or row.get("umaResolutionStatus") == "resolved"):
                return None
            prices = _as_list(row.get("outcomePrices"))
            outcomes = _as_list(row.get("outcomes"))
            yes_idx = next((i for i, o in enumerate(outcomes)
                            if str(o).strip().lower() == "yes"), 0)
            if yes_idx < len(prices):
                yp = _f(prices[yes_idx])
                if yp >= 0.99:
                    return 1
                if yp <= 0.01:
                    return 0
        return None

    # -- orderbook --------------------------------------------------------
    def fetch_orderbook(self, market_id: str) -> OrderbookSnapshot | None:
        token_id = self._resolve_token(market_id)
        if not token_id:
            return None  # unknown market -> no book (never guess the token)
        data = fetch_json(CLOB_URL, "/book", params={"token_id": token_id}, venue="Polymarket")

        def _levels(rows) -> list[OrderbookLevel]:
            # Defensive: a malformed row (non-dict) must skip, not throw past the
            # AdapterError boundary and 500 a paid call.
            out = []
            for l in rows or []:
                try:
                    out.append(OrderbookLevel(price=round(_f(l.get("price")), 4),
                                              size_usd=round(_f(l.get("size")), 2)))
                except (TypeError, ValueError, AttributeError):
                    continue
            return out[:10]

        asks = _levels(data.get("asks") if isinstance(data, dict) else None)
        bids = _levels(data.get("bids") if isinstance(data, dict) else None)
        # CLOB returns asks ascending is not guaranteed; sort for a clean top-of-book.
        asks.sort(key=lambda l: l.price)
        bids.sort(key=lambda l: l.price, reverse=True)
        return OrderbookSnapshot(
            venue=Venue.POLYMARKET,
            market_id=market_id,
            as_of=datetime.now(timezone.utc),
            yes_asks=asks,
            yes_bids=bids,
        )
