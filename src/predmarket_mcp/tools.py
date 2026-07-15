"""MCP tool implementations.

Tools are thin: they call ``deps`` (the seam to ``core/``), then format the
result for an agent. No business logic lives here. Every tool is tagged with
its tier ("free"/"paid") and carries its per-call price in ``meta`` and in its
description copy, so an agent can reason about cost before calling.

Descriptions are user-facing copy — the JSON Schema is the only documentation
the agent gets. Argument names use the vocabulary a junior engineer would use
(`event`, `min_edge`, `venue`), not internal API params.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Literal

from pydantic import Field

from fastmcp import FastMCP

from . import deps
from .billing.tiers import load_pricing, price_str
from .config import get_settings
from .provenance import provenance
from core.models import Leg, Side, Venue


# --- small formatting helpers ----------------------------------------------
def _market_dict(m) -> dict:
    return {
        "venue": m.venue.value,
        "market_id": m.market_id,
        "title": m.title,
        "category": m.category,
        "yes_price": m.yes_price,
        "no_price": m.no_price,
        "implied_probability": m.implied_probability,
        "volume_usd": m.volume_usd,
    }


def _paid_meta(tool_name: str) -> dict:
    p = load_pricing().get(tool_name)
    return {"tier": p.tier, "price_usd": p.price_usd, "currency": load_pricing().currency}


def _cost_note(tool_name: str) -> dict:
    """Cost/tier block echoed in every response so agents can reconcile."""
    p = load_pricing().get(tool_name)
    return {"tier": p.tier, "price_usd": p.price_usd}


def _client_id() -> str:
    """Stable-per-caller id for watches/alerts, from HTTP headers (else anon)."""
    try:
        from fastmcp.server.dependencies import get_http_headers

        headers = get_http_headers(include_all=True) or {}
    except Exception:
        headers = {}
    return headers.get("x-client-id") or headers.get("authorization") or "anonymous"


def register(mcp: FastMCP) -> None:
    settings = get_settings()

    # =====================================================================
    # FREE TIER  (discovery / funnel — price 0)
    # =====================================================================

    @mcp.tool(
        tags={"free"},
        meta=_paid_meta("search_markets"),
        description=(
            "Find prediction markets by keyword across venues. Returns normalized "
            "markets (venue, id, title, YES/NO price, implied probability, category). "
            "Free — start here to discover what exists before evaluating anything."
        ),
    )
    def search_markets(
        query: Annotated[str, Field(description="Keywords, e.g. 'bitcoin 100k' or 'fed rate cut'.")],
        category: Annotated[str | None, Field(description="Optional filter: politics, crypto, economics.")] = None,
        venue: Annotated[str | None, Field(description="Optional filter: polymarket or kalshi.")] = None,
    ) -> dict:
        markets = deps.search_markets(query, category, venue)
        return {
            "markets": [_market_dict(m) for m in markets],
            "count": len(markets),
            **deps.staleness(deps.now()),
            **_cost_note("search_markets"),
        }

    @mcp.tool(
        tags={"free"},
        meta=_paid_meta("list_venues"),
        description=(
            "List supported prediction-market venues, their live status and coverage. "
            "Free — call to learn which venues you can query and what they cover."
        ),
    )
    def list_venues() -> dict:
        return {
            "venues": deps.list_venues(),
            **deps.staleness(deps.now()),
            **_cost_note("list_venues"),
        }

    @mcp.tool(
        tags={"free"},
        meta=_paid_meta("track_record"),
        description=(
            "Verifiable track record for this server's calls. Reports how flagged "
            "opportunities actually performed once their markets resolved: hit_rate, "
            "mean predicted vs realized edge (edge_slippage), and a Brier score on the "
            "implied probabilities surfaced (lower is better). Free — call this to "
            "decide how much to trust find_mispricing before paying for it."
        ),
    )
    def track_record() -> dict:
        metrics = deps.track_record_metrics()
        return {
            "track_record": metrics,
            "provenance": provenance(metrics),
            **deps.staleness(deps.now()),
            **_cost_note("track_record"),
        }

    @mcp.tool(
        tags={"free"},
        meta=_paid_meta("evaluate_market"),
        description=(
            "Get normalized YES/NO prices, implied probability, and orderbook depth "
            "for one market. FREE TIER RETURNS DELAYED DATA (~60s; see `as_of` / "
            "`data_age_seconds`). Realtime is available on the paid tools. Call to "
            "judge whether a single market is worth acting on."
        ),
    )
    def evaluate_market(
        venue: Annotated[str, Field(description="polymarket or kalshi.")],
        market_id: Annotated[str, Field(description="Market id from search_markets.")],
    ) -> dict:
        market = deps.get_market(venue, market_id)
        if market is None:
            return {"error": "market_not_found", "venue": venue, "market_id": market_id}
        book = deps.get_orderbook(venue, market_id)
        # Free tier: intentionally delay the snapshot and mark it as such.
        delay = timedelta(seconds=settings.free_tier_delay_seconds)
        as_of = deps.now() - delay
        depth = None
        if book:
            depth = {
                "yes_asks": [{"price": l.price, "size_usd": l.size_usd} for l in book.yes_asks[:5]],
                "yes_bids": [{"price": l.price, "size_usd": l.size_usd} for l in book.yes_bids[:5]],
            }
        return {
            "market": _market_dict(market),
            "orderbook": depth,
            "realtime": False,
            **deps.staleness(as_of),
            **_cost_note("evaluate_market"),
        }

    # =====================================================================
    # PAID TIER  (per-call revenue — realtime)
    # =====================================================================

    @mcp.tool(
        tags={"paid"},
        meta=_paid_meta("find_mispricing"),
        description=(
            "Flagship scanner. Scans LIVE opportunities whose REALIZABLE EDGE (after "
            "fees, gas and slippage — never gross) exceeds `min_edge` (e.g. 0.02 = 2%). "
            "Detects cross_venue spreads, single-market bundles, and dutch_book "
            "(combinatorial arb across mutually-exclusive outcomes). Each result is "
            "risk-adjusted: holding_days, annualized_edge, resolution_risk. Realtime. "
            f"Costs {price_str('find_mispricing')} per call — check price before calling."
        ),
    )
    def find_mispricing(
        min_edge: Annotated[float, Field(ge=0, le=1, description="Minimum realizable edge, e.g. 0.02 for 2%.")],
        kind: Annotated[Literal["bundle", "cross_venue", "dutch_book"] | None, Field(description="Optional: restrict to one opportunity kind.")] = None,
        category: Annotated[str | None, Field(description="Optional filter: politics, crypto, economics.")] = None,
    ) -> dict:
        ops = deps.scan_opportunities(min_edge, kind, category)
        deps.record_flagged(ops)  # track record: flag now, reconcile at resolution
        opportunities = [
            {
                "kind": o.kind.value,
                "title": o.title,
                "category": o.category,
                "realizable_edge": o.realizable_edge,
                "annualized_edge": o.annualized_edge,
                "holding_days": o.holding_days,
                "resolution_risk": o.resolution_risk,
                "max_size_usd": o.max_size_usd,
                "legs": [{"venue": l.venue.value, "market_id": l.market_id, "side": l.side.value} for l in o.legs],
            }
            for o in ops
        ]
        return {
            "opportunities": opportunities,
            "count": len(ops),
            "min_edge": min_edge,
            "realtime": True,
            "provenance": provenance(opportunities),  # tamper-evident signature
            **deps.staleness(deps.now()),
            **_cost_note("find_mispricing"),
        }

    @mcp.tool(
        tags={"paid"},
        meta=_paid_meta("compare_across_venues"),
        description=(
            "For one real-world event, find the matching markets across venues and "
            "report the spread and direction (which venue is cheaper on YES). Uses the "
            "cross-venue matcher and returns match confidence. Realtime. "
            f"Costs {price_str('compare_across_venues')} per call."
        ),
    )
    def compare_across_venues(
        event: Annotated[str, Field(description="Plain-language event, e.g. 'bitcoin above 100k end of 2026'.")],
    ) -> dict:
        pair = deps.match_event(event)
        if pair is None:
            return {"matched": False, "event": event, **_cost_note("compare_across_venues")}
        cheaper = pair.a if pair.a.yes_price <= pair.b.yes_price else pair.b
        return {
            "matched": True,
            "event": pair.event,
            "confidence": pair.confidence,
            "spread": round(pair.spread, 4),
            "cheaper_yes_venue": cheaper.venue.value,
            "markets": [_market_dict(pair.a), _market_dict(pair.b)],
            "realtime": True,
            **deps.staleness(deps.now()),
            **_cost_note("compare_across_venues"),
        }

    @mcp.tool(
        tags={"paid"},
        meta=_paid_meta("estimate_execution"),
        description=(
            "Before you act: given these legs and a dollar size, compute the REALIZABLE "
            "EDGE AFTER fees, gas and slippage from current orderbook depth. Returns "
            "fillable size, average fill price and net edge so you can decide before "
            f"trading. Costs {price_str('estimate_execution')} per call."
        ),
    )
    def estimate_execution(
        legs: Annotated[list[Leg], Field(description="Legs to execute. Each: {venue, market_id, side: yes|no}.")],
        size_usd: Annotated[float, Field(gt=0, description="Total dollar size you intend to execute.")],
    ) -> dict:
        est = deps.realizable_edge(legs, size_usd)
        return {
            "estimate": est.model_dump(),
            "realtime": True,
            **deps.staleness(deps.now()),
            **_cost_note("estimate_execution"),
        }

    @mcp.tool(
        tags={"paid"},
        meta=_paid_meta("get_market_history"),
        description=(
            "Historical price/spread time series for one market from the history store. "
            "Use ISO-8601 timestamps for `from_ts` and `to_ts`. A data product — "
            f"costs {price_str('get_market_history')} per call (scales with range)."
        ),
    )
    def get_market_history(
        venue: Annotated[str, Field(description="polymarket or kalshi.")],
        market_id: Annotated[str, Field(description="Market id from search_markets.")],
        from_ts: Annotated[str, Field(description="Start time, ISO-8601, e.g. 2026-06-01T00:00:00Z.")],
        to_ts: Annotated[str, Field(description="End time, ISO-8601.")],
    ) -> dict:
        try:
            frm = _parse_iso(from_ts)
            to = _parse_iso(to_ts)
        except ValueError:
            return {"error": "bad_timestamp", "hint": "Use ISO-8601, e.g. 2026-06-01T00:00:00Z."}
        points = deps.get_history(venue, market_id, frm, to)
        return {
            "venue": venue,
            "market_id": market_id,
            "from_ts": frm.isoformat(),
            "to_ts": to.isoformat(),
            "points": [{"ts": p.ts.isoformat(), "yes_price": p.yes_price, "spread": p.spread} for p in points],
            "count": len(points),
            **deps.staleness(deps.now()),
            **_cost_note("get_market_history"),
        }

    @mcp.tool(
        tags={"paid"},
        meta=_paid_meta("watch"),
        description=(
            "Subscribe to opportunities instead of polling for them. Registers a watch "
            "for opportunities whose realizable_edge crosses `min_edge` (optionally "
            "filtered by kind/category/event). Returns a watch_id and any immediate "
            "matches; retrieve later ones with poll_alerts. "
            f"Costs {price_str('watch')} per subscription."
        ),
    )
    def watch(
        min_edge: Annotated[float, Field(ge=0, le=1, description="Alert when realizable edge crosses this, e.g. 0.02.")],
        kind: Annotated[Literal["bundle", "cross_venue", "dutch_book"] | None, Field(description="Optional: only this opportunity kind.")] = None,
        category: Annotated[str | None, Field(description="Optional filter: politics, crypto, economics.")] = None,
        event: Annotated[str | None, Field(description="Optional: only opportunities whose title contains this text.")] = None,
    ) -> dict:
        result = deps.create_watch(_client_id(), min_edge, category, kind, event)
        return {**result, "realtime": True, **deps.staleness(deps.now()), **_cost_note("watch")}

    @mcp.tool(
        tags={"paid"},
        meta=_paid_meta("poll_alerts"),
        description=(
            "Retrieve opportunities that fired against your watches since you last "
            "polled (each alert delivered once). Cheap — poll on your own cadence "
            f"instead of re-running the scanner. Costs {price_str('poll_alerts')} per call."
        ),
    )
    def poll_alerts() -> dict:
        alerts = deps.poll_alerts(_client_id())
        return {
            "alerts": alerts,
            "count": len(alerts),
            "realtime": True,
            **deps.staleness(deps.now()),
            **_cost_note("poll_alerts"),
        }


def _parse_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt
