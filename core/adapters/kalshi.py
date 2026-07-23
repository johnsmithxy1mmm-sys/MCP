"""Kalshi adapter.

Data source: https://api.elections.kalshi.com/trade-api/v2
* GET /markets                      — market metadata + best bid/ask (in cents)
* GET /markets/{ticker}/orderbook   — depth (price in cents, size in contracts)

Kalshi prices are integer cents (0..100); we normalize to 0..1. The markets list
is public; private endpoints (orderbook) require RSA-PSS request signing:

  KALSHI_API_KEY_ID        access key id
  KALSHI_PRIVATE_KEY       RSA private key PEM (or KALSHI_PRIVATE_KEY_PATH)

If those aren't set, a legacy KALSHI_API_KEY bearer token is used as a fallback.
"""

from __future__ import annotations

import base64
import os
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

from ..models import Market, OrderbookLevel, OrderbookSnapshot, Venue
from .base import VenueAdapter, _matches_query, fetch_json

KALSHI_URL = os.getenv("KALSHI_API_URL", "https://api.elections.kalshi.com/trade-api/v2")
_BASE_PATH = urlparse(KALSHI_URL).path.rstrip("/")  # e.g. /trade-api/v2


def _cents_to_prob(cents) -> float:
    try:
        return round(float(cents) / 100.0, 4)
    except (TypeError, ValueError):
        return 0.0


def _load_private_key():
    """Load the RSA private key from env (PEM string or path), or None."""
    pem = os.getenv("KALSHI_PRIVATE_KEY")
    if not pem:
        path = os.getenv("KALSHI_PRIVATE_KEY_PATH")
        if path and os.path.exists(path):
            pem = open(path).read()
    if not pem:
        return None
    try:
        from cryptography.hazmat.primitives.serialization import load_pem_private_key

        return load_pem_private_key(pem.encode(), password=None)
    except Exception:
        return None  # cryptography missing or bad key -> fall back


def _auth_headers(method: str, rel_path: str) -> dict:
    """Signed Kalshi headers for `method` + the request path.

    Kalshi signs `timestamp_ms + METHOD + full_path` with RSA-PSS/SHA-256.
    Falls back to a legacy bearer token, then to no auth (public endpoints).
    """
    key_id = os.getenv("KALSHI_API_KEY_ID")
    private_key = _load_private_key()
    if key_id and private_key is not None:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        ts = str(int(time.time() * 1000))
        message = ts + method.upper() + _BASE_PATH + rel_path
        signature = private_key.sign(
            message.encode(),
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": key_id,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
            "KALSHI-ACCESS-TIMESTAMP": ts,
        }
    legacy = os.getenv("KALSHI_API_KEY")
    return {"Authorization": f"Bearer {legacy}"} if legacy else {}


class KalshiAdapter(VenueAdapter):
    venue = Venue.KALSHI

    def fetch_markets(self, query: str | None = None, limit: int = 50) -> list[Market]:
        data = fetch_json(
            KALSHI_URL, "/markets",
            params={"limit": limit, "status": "open"},
            headers=_auth_headers("GET", "/markets"), venue="Kalshi",
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
        # Kalshi `volume` is a CONTRACT count, not dollars. Each contract settles
        # at $1, so USD notional traded ~= contracts * price (a contract's cost).
        contracts = float(row.get("volume") or 0)
        volume_usd = round(contracts * yes, 2) if contracts and yes else None
        # Kalshi groups related markets under an event ticker (often the mutually-
        # exclusive brackets of one event).
        event_group = str(row.get("event_ticker") or "").strip() or None
        return Market(
            venue=Venue.KALSHI,
            market_id=ticker,
            title=str(row.get("title") or row.get("subtitle") or ticker).strip(),
            category=(str(row["category"]).lower() if row.get("category") else None),
            yes_price=yes,
            no_price=round(1.0 - yes, 4),
            volume_usd=volume_usd,
            event_group=event_group,
        )

    def fetch_resolution(self, market_id: str) -> int | None:
        """Settled outcome from Kalshi: status settled/finalized + result yes/no."""
        try:
            data = fetch_json(KALSHI_URL, f"/markets/{market_id}",
                              headers=_auth_headers("GET", f"/markets/{market_id}"),
                              venue="Kalshi")
        except Exception:
            return None
        row = (data or {}).get("market") or data or {}
        if str(row.get("status", "")).lower() not in ("settled", "finalized", "closed"):
            return None
        result = str(row.get("result", "")).lower()
        if result == "yes":
            return 1
        if result == "no":
            return 0
        return None

    def fetch_orderbook(self, market_id: str) -> OrderbookSnapshot | None:
        path = f"/markets/{market_id}/orderbook"
        data = fetch_json(
            KALSHI_URL, path,
            headers=_auth_headers("GET", path), venue="Kalshi",
        )
        book = (data or {}).get("orderbook", {}) or {}
        # Kalshi returns YES bids under "yes" and NO bids under "no"; a YES ask is
        # the complement of a NO bid: price_yes_ask = 1 - price_no_bid.
        yes_levels = book.get("yes") or []
        no_levels = book.get("no") or []
        # Kalshi depth sizes are CONTRACT counts; USD notional at a level is
        # contracts * price (what it costs to take that price level).
        # Defensive per-level parse: one malformed row from the venue must skip,
        # not throw past the AdapterError boundary and 500 a paid call.
        bids = []
        for level in yes_levels:
            try:
                p, s = level[0], level[1]
                price = _cents_to_prob(p)
                bids.append(OrderbookLevel(price=price, size_usd=round(float(s) * price, 2)))
            except (TypeError, ValueError, IndexError):
                continue
        asks = []
        for level in no_levels:
            try:
                p, s = level[0], level[1]
                price = round(1.0 - _cents_to_prob(p), 4)
                asks.append(OrderbookLevel(price=price, size_usd=round(float(s) * price, 2)))
            except (TypeError, ValueError, IndexError):
                continue
        bids.sort(key=lambda l: l.price, reverse=True)
        asks.sort(key=lambda l: l.price)
        return OrderbookSnapshot(
            venue=Venue.KALSHI,
            market_id=market_id,
            as_of=datetime.now(timezone.utc),
            yes_asks=asks[:10],
            yes_bids=bids[:10],
        )
