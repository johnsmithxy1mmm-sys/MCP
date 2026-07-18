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
        "event://{entity}",
        description=(
            "Everything the markets say about one subject: all related markets "
            "across venues, their logical relations (same_event, implies, "
            "term_neighbor), and any transitive entailment violations (mispricings "
            "that leak across a middle rung). Free — a map of an event's whole "
            "market structure. e.g. event://bitcoin%20100k"
        ),
    )
    def event_resource(entity: str) -> dict:
        return {**deps.event_view(entity), **deps.staleness(deps.now())}

    @mcp.resource(
        "conditional://{event}",
        description=(
            "Market-implied CONDITIONAL probabilities P(A|B) among related markets "
            "for an event — a dependency graph agents can reason over ('if B, then "
            "how likely is A?'). Estimated with a Gaussian copula (correlation from "
            "price history) and exact logical overrides (entailment => 1, mutual "
            "exclusion => 0). Free. e.g. conditional://bitcoin"
        ),
    )
    def conditional_resource(event: str) -> dict:
        return {**deps.conditional_view(event), **deps.staleness(deps.now())}

    @mcp.resource(
        "distribution://{entity}",
        description=(
            "The market's full IMPLIED DISTRIBUTION for a numeric event, "
            "reconstructed from a ladder of 'X above K' threshold markets — the "
            "prediction-market analogue of an options vol surface. Returns the "
            "survival curve P(X>k), percentiles (p10/median/p90), implied mean, and "
            "the implied probability at any strike. Free. e.g. distribution://bitcoin"
        ),
    )
    def distribution_resource(entity: str) -> dict:
        return {**deps.distribution_view(entity), **deps.staleness(deps.now())}

    @mcp.resource(
        "outcomes://{event}",
        description=(
            "Native multi-outcome view of an event with N mutually-exclusive "
            "outcomes (election, bracketed number): DE-VIGGED fair probabilities "
            "(margin removed so they sum to 1), the overround, the favorite, and a "
            "completeness-aware full dutch book (an under-round is only arbitrage if "
            "the outcome set is complete). Free. e.g. outcomes://2028-president"
        ),
    )
    def outcomes_resource(event: str) -> dict:
        return {**deps.outcome_view(event), **deps.staleness(deps.now())}

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
