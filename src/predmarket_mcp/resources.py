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
        "alerts://me",
        description=(
            "Peek the alerts currently queued for YOUR watches WITHOUT consuming "
            "them (poll_alerts drains; this only reads). Always scoped to you — "
            "there is no URI that names another caller. Use for a "
            "subscribe/refresh view; pair with the webhook push for delivery."
        ),
    )
    def alerts_resource() -> dict:
        # No client_id parameter by design: a URI that names a principal invites
        # IDOR, and the previous 'does it match your header?' check compared two
        # caller-controlled values (audit INV-002).
        from .identity import caller_identity, personal_access_error

        identity = caller_identity()
        denied = personal_access_error(identity)
        if denied is not None:
            return denied
        alerts = deps.peek_alerts(identity.id)
        return {
            **identity.as_dict(),
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
        "scenario://scan/{spec}",
        description=(
            "WHAT-IF engine: pin one or more markets to a hypothetical outcome and "
            "see how every related market moves. `spec` is a comma list of "
            "`market_id:yes|no`, e.g. scenario://scan/pm-btc-150k-2026:yes. Returns "
            "each affected market's prior, posterior, and shift, with the drivers "
            "(logical vs correlation). Free — a prediction-market stress test."
        ),
    )
    def scenario_resource(spec: str) -> dict:
        return {**deps.simulate_scenario(spec), **deps.staleness(deps.now())}

    @mcp.resource(
        "leaderboard://top",
        description=(
            "Public PROOF-OF-ALPHA leaderboard: agents ranked by REAL, "
            "resolution-verified paper P&L from positions they committed via "
            "estimate_execution(commit=true). Handles are anonymized; the ranking is "
            "server-signed. Free — an independent notary of agent performance."
        ),
    )
    def leaderboard_resource() -> dict:
        from .provenance import provenance as _prov

        board = deps.leaderboard()
        return {**board, "provenance": _prov(board["ranked"]),
                **deps.staleness(deps.now())}

    @mcp.resource(
        "portfolio://me",
        description=(
            "YOUR own paper portfolio + leaderboard rank: every position you "
            "committed via estimate_execution(commit=true), with realized P&L "
            "once resolved. Always scoped to you — there is no URI that names "
            "another caller. Free."
        ),
    )
    def portfolio_resource() -> dict:
        # A portfolio reveals an agent's strategy (markets, sides, sizes, P&L),
        # so it is addressed only as 'me' and resolved from a derived identity.
        from .identity import caller_identity, personal_access_error

        identity = caller_identity()
        denied = personal_access_error(identity)
        if denied is not None:
            return denied
        return {**identity.as_dict(), **deps.client_portfolio(identity.id),
                **deps.staleness(deps.now())}

    @mcp.resource(
        "maker://{venue}/{market_id}",
        description=(
            "MARKET-MAKER advisor for one market: where to post a two-sided limit "
            "quote to earn the spread (recommended_bid/ask, half_spread), how to "
            "skew it, and a `pull_quotes` guard that says step aside when the flow "
            "looks informed. Prices adverse-selection risk from microstructure + "
            "quote quality. Free — intelligence only, never posts orders. "
            "e.g. maker://kalshi/kx-btc-100k-eoy26"
        ),
    )
    def maker_resource(venue: str, market_id: str) -> dict:
        adv = deps.maker_advice(venue, market_id)
        if adv is None:
            return {"error": "market_not_found", "venue": venue, "market_id": market_id}
        return {"venue": venue, "market_id": market_id, **adv,
                **deps.staleness(deps.now())}

    @mcp.resource(
        "house://{venue}/{market_id}",
        description=(
            "The SERVER'S OWN probability for one market (not the raw price): "
            "calibration, options-implied, accuracy-weighted consensus and "
            "microstructure fused into one house_probability, with a confidence and "
            "its edge_vs_market. Free. Its accuracy is graded publicly — see "
            "track_record.house_forecast. e.g. house://kalshi/kx-btc-100k-eoy26"
        ),
    )
    def house_resource(venue: str, market_id: str) -> dict:
        view = deps.house_view(venue, market_id)
        if view is None:
            return {"error": "market_not_found", "venue": venue, "market_id": market_id}
        return {"venue": venue, "market_id": market_id, **view,
                **deps.staleness(deps.now())}

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
