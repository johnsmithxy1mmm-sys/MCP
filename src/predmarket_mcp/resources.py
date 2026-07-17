"""Read-only resources.

``market://{venue}/{market_id}`` exposes the same snapshot as ``evaluate_market``
for agents that prefer resources over tool calls. Free-tier freshness rules apply
(delayed data, marked with ``as_of`` / ``data_age_seconds``).
"""

from __future__ import annotations

from datetime import timedelta

from fastmcp import FastMCP

from . import deps
from .config import get_settings


def register(mcp: FastMCP) -> None:
    settings = get_settings()

    @mcp.resource(
        "alerts://{client_id}",
        description=(
            "Peek the alerts currently queued for YOUR watches WITHOUT consuming "
            "them (poll_alerts drains; this only reads). The client_id must match "
            "your own caller identity. Use for a subscribe/refresh view; pair "
            "with the webhook push for true delivery."
        ),
    )
    def alerts_resource(client_id: str) -> dict:
        from .tools import _client_id as caller_id

        # Alerts can carry paid intelligence — only the owning caller may peek.
        if client_id != caller_id():
            return {"error": "forbidden", "detail": "client_id does not match caller"}
        alerts = deps.peek_alerts(client_id)
        return {
            "client_id": client_id,
            "alerts": alerts,
            "count": len(alerts),
            "note": "Undelivered alerts; call poll_alerts to consume them.",
            **deps.staleness(deps.now()),
        }

    @mcp.resource(
        "market://{venue}/{market_id}",
        description=(
            "Snapshot of one prediction market (normalized YES/NO prices, implied "
            "probability, top-of-book depth). Free tier — data is delayed ~60s; see "
            "`as_of` / `data_age_seconds`."
        ),
    )
    def market_resource(venue: str, market_id: str) -> dict:
        market = deps.get_market(venue, market_id)
        if market is None:
            return {"error": "market_not_found", "venue": venue, "market_id": market_id}
        book = deps.get_orderbook(venue, market_id)
        as_of = deps.now() - timedelta(seconds=settings.free_tier_delay_seconds)
        depth = None
        if book:
            depth = {
                "yes_asks": [{"price": l.price, "size_usd": l.size_usd} for l in book.yes_asks[:5]],
                "yes_bids": [{"price": l.price, "size_usd": l.size_usd} for l in book.yes_bids[:5]],
            }
        return {
            "venue": market.venue.value,
            "market_id": market.market_id,
            "title": market.title,
            "category": market.category,
            "yes_price": market.yes_price,
            "no_price": market.no_price,
            "implied_probability": market.implied_probability,
            "volume_usd": market.volume_usd,
            "orderbook": depth,
            "realtime": False,
            **deps.staleness(as_of),
        }
