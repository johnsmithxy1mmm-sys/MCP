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

from fastmcp import Context, FastMCP

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
    """Stable-per-caller id for watches/alerts — never a shared "anonymous".

    A shared bucket would let one caller drain another's alerts (deliver-once).
    Prefer x-client-id; else hash the auth token (never store it raw); else
    derive an ephemeral id from ip+user-agent so distinct callers stay isolated.
    """
    import hashlib

    try:
        from fastmcp.server.dependencies import get_http_headers

        headers = get_http_headers(include_all=True) or {}
    except Exception:
        headers = {}
    cid = headers.get("x-client-id")
    if cid:
        return cid
    auth = headers.get("authorization")
    if auth:
        return "auth-" + hashlib.sha256(auth.encode()).hexdigest()[:16]
    ip = headers.get("x-forwarded-for", "").split(",")[0].strip()
    ua = headers.get("user-agent", "")
    if ip or ua:
        return "anon-" + hashlib.sha256(f"{ip}|{ua}".encode()).hexdigest()[:16]
    return "anon-local"


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
            "Verifiable track record: how flagged opportunities resolved — hit_rate, "
            "edge_slippage, Brier, Merkle commitment. Plus `house_forecast`: the "
            "server's OWN probability vs the market's, Brier-scored (brier_edge>0 = "
            "house beats the market). Free — how much to trust this server."
        ),
    )
    def track_record() -> dict:
        metrics = deps.track_record_metrics()
        house = deps.house_forecast_metrics()      # house Brier vs market Brier (K1)
        commitment = deps.track_record_commitment()  # Merkle root over resolved records
        signed = {**metrics, "house_forecast": house, "commitment": commitment}
        return {
            "track_record": metrics,
            "house_forecast": house,
            "commitment": commitment,
            "provenance": provenance(signed),
            **deps.staleness(deps.now()),
            **_cost_note("track_record"),
        }

    @mcp.tool(
        tags={"free"},
        meta=_paid_meta("evaluate_market"),
        description=(
            "Normalized YES/NO prices + implied probability for one market. FREE TIER "
            "RETURNS A GENUINELY DELAYED PRICE (~60s, from history; `as_of` true, "
            "`delayed: true`), plus history-calibrated probability and the server's own "
            "fused `house` probability (with edge_vs_market). Realtime prices + depth "
            "are paid (estimate_execution / find_mispricing)."
        ),
    )
    def evaluate_market(
        venue: Annotated[str, Field(description="polymarket or kalshi.")],
        market_id: Annotated[str, Field(description="Market id from search_markets.")],
    ) -> dict:
        market = deps.get_market(venue, market_id)
        if market is None:
            return {"error": "market_not_found", "venue": venue, "market_id": market_id}
        now = deps.now()
        delay = settings.free_tier_delay_seconds
        # The server's own fused forecast (K1) — recorded for public Brier grading.
        house = deps.house_view(venue, market_id)
        # Real-money cross-check vs the options market (opt-in; None when off).
        options = deps.options_divergence(market)
        # Microstructure / informed-flow read from recent history (None if sparse).
        micro = deps.microstructure(venue, market_id)
        # Honest delay: serve a real historical point at least `delay` seconds
        # old, with its true timestamp — never backdate live data.
        points = deps.get_history(
            venue, market_id, now - timedelta(hours=6), now - timedelta(seconds=delay)
        )
        if points:
            p = points[-1]
            yes = p.yes_price
            return {
                "market": {
                    "venue": market.venue.value,
                    "market_id": market.market_id,
                    "title": market.title,
                    "category": market.category,
                    "yes_price": yes,
                    "no_price": round(1.0 - yes, 4),
                    "implied_probability": yes,
                },
                # History-calibrated probability (identity until enough outcomes resolve).
                "calibration": deps.calibrate_probability(yes, market.category),
                # The server's own fused probability + its disagreement with the market.
                **({"house": house} if house else {}),
                "quality": deps.quote_quality(yes, market.volume_usd, delay),
                **({"options": options} if options else {}),
                **({"microstructure": micro} if micro else {}),
                "delayed": True,
                "realtime": False,
                "note": f"Free tier: price ~{delay}s delayed from the history store; "
                        "realtime + orderbook depth are paid.",
                **deps.staleness(p.ts),
                **_cost_note("evaluate_market"),
            }
        # No delayed snapshot exists yet — be honest, don't fabricate staleness.
        return {
            "market": _market_dict(market),
            "calibration": deps.calibrate_probability(market.yes_price, market.category),
            **({"house": house} if house else {}),
            **({"options": options} if options else {}),
            "delayed": False,
            "realtime": False,
            "note": "No delayed snapshot available yet; showing the latest with an "
                    "accurate as_of (not backdated).",
            **deps.staleness(now),
            **_cost_note("evaluate_market"),
        }

    # =====================================================================
    # PAID TIER  (per-call revenue — realtime)
    # =====================================================================

    @mcp.tool(
        tags={"paid"},
        meta=_paid_meta("find_mispricing"),
        description=(
            "Flagship scanner. LIVE opportunities whose REALIZABLE EDGE (net of fees/"
            "gas/slippage) exceeds `min_edge` (0.02=2%). Kinds: cross_venue, bundle, "
            "dutch_book, entailment (risk-free logic/term-structure violations). Ranked "
            "by expected_value; each carries survival, velocity, adverse (trap) and "
            "risk fields. rank=velocity for fastest turnover; bankroll_usd for a "
            f"rotation_plan; audit for signed evidence. Costs {price_str('find_mispricing')}/call."
        ),
    )
    def find_mispricing(
        min_edge: Annotated[float, Field(ge=0, le=1, description="Minimum realizable edge, e.g. 0.02 for 2%.")],
        kind: Annotated[Literal["bundle", "cross_venue", "dutch_book", "entailment"] | None, Field(description="Optional: restrict to one opportunity kind.")] = None,
        category: Annotated[str | None, Field(description="Optional filter: politics, crypto, economics.")] = None,
        rank: Annotated[Literal["expected_value", "velocity"], Field(description="velocity = fastest capital turnover first.")] = "expected_value",
        bankroll_usd: Annotated[float | None, Field(default=None, gt=0, description="With horizon_days, adds a rotation_plan.")] = None,
        horizon_days: Annotated[float, Field(gt=0, le=365, description="Rotation-plan horizon.")] = 30,
        audit: Annotated[bool, Field(description="Attach a signed audit_bundle_id, verifiable at /audit/{id}.")] = False,
    ) -> dict:
        ops = deps.scan_opportunities(min_edge, kind, category)
        ops = deps.enrich_resolution_risk(ops)  # LLM resolution-risk (if enabled)
        # Rank by expected value (edge x survival), learning opportunity lifespans.
        ops = deps.rank_by_expected_value(ops, observe_full=(kind is None and category is None))
        ops = deps.enrich_adverse(ops)  # adverse-selection trap score (J1)
        if rank == "velocity":
            ops = deps.rank_by_velocity(ops)  # fast-turnover strategy
        deps.record_flagged(ops)  # track record: flag now, reconcile at resolution
        opportunities = []
        for o in ops:
            row = {
                "kind": o.kind.value,
                "title": o.title,
                "category": o.category,
                "realizable_edge": o.realizable_edge,
                "annualized_edge": o.annualized_edge,
                "holding_days": o.holding_days,
                "resolution_risk": o.resolution_risk,
                "fair_value": o.fair_value,
                "survival_probability": o.survival_probability,
                "expected_value": o.expected_value,
                "velocity": o.velocity,
                "adverse": o.adverse,
                "max_size_usd": o.max_size_usd,
                "legs": [{"venue": l.venue.value, "market_id": l.market_id, "side": l.side.value} for l in o.legs],
            }
            if audit:  # opt-in signed evidence bundle, verifiable at /audit/{id}
                row["audit_bundle_id"] = deps.build_audit_bundle(o)["bundle_id"]
            opportunities.append(row)
        response = {
            "opportunities": opportunities,
            "count": len(ops),
            "min_edge": min_edge,
            "rank": rank,
            "realtime": True,
            "provenance": provenance(opportunities),  # tamper-evident signature
            **deps.staleness(deps.now()),
            **_cost_note("find_mispricing"),
        }
        if bankroll_usd:
            # Capital-rotation plan: deploy across short cycles, recycle on resolve.
            response["rotation_plan"] = deps.rotation_plan(ops, bankroll_usd, horizon_days)
        return response

    @mcp.tool(
        tags={"paid"},
        meta=_paid_meta("compare_across_venues"),
        description=(
            "Match one event across venues: spread, cheaper-YES venue, consensus fair "
            "value, history-calibrated match confidence, and settlement basis_risk — "
            "whether the two resolve by the same rules. same_contract=false means the "
            "spread is a bet on rulebooks, not free money. Realtime. "
            f"Costs {price_str('compare_across_venues')} per call."
        ),
    )
    async def compare_across_venues(
        event: Annotated[str, Field(description="Plain-language event, e.g. 'bitcoin above 100k end of 2026'.")],
        ctx: Context | None = None,
    ) -> dict:
        pair = deps.match_event(event)
        if pair is None:
            return {"matched": False, "event": event, **_cost_note("compare_across_venues")}
        cheaper = pair.a if pair.a.yes_price <= pair.b.yes_price else pair.b
        fair = deps.assess_fair_value([pair.a, pair.b])
        basis = deps.assess_basis(pair.a, pair.b)
        # K3: refine basis risk with the CLIENT's own model via MCP sampling —
        # deepest rulebook analysis at the caller's expense. Opt-in + degrades.
        sampled = await _sample_basis(ctx, pair.a, pair.b)
        if sampled is not None:
            basis = sampled
        # Learn from ground truth: record the match, report history-calibrated confidence.
        deps.record_match(pair.a.market_id, pair.b.market_id, pair.confidence)
        match_conf = deps.match_confidence(pair.confidence)
        return {
            "matched": True,
            "event": pair.event,
            "confidence": pair.confidence,
            "calibrated_confidence": match_conf,
            "spread": round(pair.spread, 4),
            "cheaper_yes_venue": cheaper.venue.value,
            # Consensus fair value + which venue is rich/cheap vs it (not just the gap).
            "fair_value": fair["fair_value"],
            "fair_value_confidence": fair["confidence"],
            "deviations": fair["venues"],
            # Best-estimate weighted by each venue's historical accuracy (J4).
            "meta_consensus": deps.meta_consensus([pair.a, pair.b]),
            # Settlement basis risk: is the spread real, or a bet on which rulebook wins?
            "basis_risk": basis["basis_risk"],
            "same_contract": basis["same_contract"],
            "resolution_differences": basis.get("differences", []),
            "markets": [_market_dict(pair.a), _market_dict(pair.b)],
            "realtime": True,
            **deps.staleness(deps.now()),
            **_cost_note("compare_across_venues"),
        }

    @mcp.tool(
        tags={"paid"},
        meta=_paid_meta("estimate_execution"),
        description=(
            "Before you act: given legs + a dollar size, compute REALIZABLE EDGE after "
            "fees, gas and slippage from live depth — fillable size, avg fill price, net "
            "edge. Optionally pass `bankroll_usd` + `fair_value` for the Kelly stake and "
            f"a market-impact curve. Costs {price_str('estimate_execution')} per call."
        ),
    )
    def estimate_execution(
        legs: Annotated[list[Leg], Field(description="Legs to execute. Each: {venue, market_id, side: yes|no}.")],
        size_usd: Annotated[float, Field(gt=0, description="Total dollar size you intend to execute.")],
        bankroll_usd: Annotated[float | None, Field(default=None, gt=0, description="Optional: your bankroll, for a Kelly-sized recommendation.")] = None,
        fair_value: Annotated[float | None, Field(default=None, ge=0, le=1, description="Optional: your fair YES probability, e.g. 0.62, for Kelly sizing.")] = None,
    ) -> dict:
        est = deps.realizable_edge(legs, size_usd)
        sizing = deps.execution_sizing(legs, size_usd, bankroll_usd, fair_value)
        return {
            "estimate": est.model_dump(),
            "sizing": sizing,
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
            "Subscribe instead of polling: registers a watch for opportunities whose "
            "realizable_edge crosses `min_edge` (optional kind/category/event filters). "
            "Returns a watch_id + immediate matches; later ones via poll_alerts. "
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
        meta=_paid_meta("simulate_strategy"),
        description=(
            "Backtest a mean-reversion rule over RECORDED history with honest costs: "
            "long YES when price dips `entry_edge` below its trailing mean, exit at "
            "`exit_edge`. Returns trades, hit_rate, total_return, sharpe, max_drawdown "
            "+ an out-of-sample walk-forward check (overfitting detector). "
            f"Costs {price_str('simulate_strategy')} per call."
        ),
    )
    def simulate_strategy(
        venue: Annotated[str, Field(description="polymarket or kalshi.")],
        market_id: Annotated[str, Field(description="Market id from search_markets.")],
        from_ts: Annotated[str, Field(description="Backtest start, ISO-8601.")],
        to_ts: Annotated[str, Field(description="Backtest end, ISO-8601.")],
        entry_edge: Annotated[float, Field(ge=0, le=1, description="Enter when price is this far below the trailing mean, e.g. 0.05.")] = 0.05,
        exit_edge: Annotated[float, Field(ge=0, le=1, description="Exit when price recovers to within this of the mean, e.g. 0.01.")] = 0.01,
        window: Annotated[int, Field(ge=2, le=500, description="Trailing-mean lookback in points.")] = 10,
        size_usd: Annotated[float, Field(gt=0, description="Position size per trade.")] = 1000.0,
    ) -> dict:
        from core.backtest import BacktestParams

        try:
            frm = _parse_iso(from_ts)
            to = _parse_iso(to_ts)
        except ValueError:
            return {"error": "bad_timestamp", "hint": "Use ISO-8601, e.g. 2026-06-01T00:00:00Z."}
        try:
            venue_enum = Venue(venue)  # reject typos instead of silently guessing
        except ValueError:
            return {"error": "unknown_venue",
                    "hint": "Use one of: " + ", ".join(v.value for v in Venue)}
        params = BacktestParams(
            entry_edge=entry_edge, exit_edge=exit_edge, window=window,
            size_usd=size_usd, venue=venue_enum,
        )
        result = deps.backtest_strategy(venue, market_id, frm, to, params)
        return {
            "venue": venue, "market_id": market_id,
            "params": {"entry_edge": entry_edge, "exit_edge": exit_edge,
                       "window": window, "size_usd": size_usd},
            "result": result,
            "realtime": False,
            **deps.staleness(deps.now()),
            **_cost_note("simulate_strategy"),
        }

    @mcp.tool(
        tags={"paid"},
        meta=_paid_meta("assess_portfolio"),
        description=(
            "Portfolio risk manager for an agent running a book. From your current "
            "legs: net exposure grouped by event (many legs can be one concentrated "
            "bet), cross-market correlation from history, a correlation-adjusted "
            "portfolio Kelly stake (pass `bankroll_usd` + `fair_values`), and concrete "
            "hedges on other venues. Pass `scenario` (e.g. 'pm-fed-cut-sep:no') to "
            f"stress the book under a hypothetical. Costs {price_str('assess_portfolio')} per call."
        ),
    )
    def assess_portfolio(
        legs: Annotated[list[Leg], Field(description="Your current positions. Each: {venue, market_id, side: yes|no}.")],
        bankroll_usd: Annotated[float | None, Field(default=None, gt=0, description="Optional: bankroll, for a Kelly-sized recommendation.")] = None,
        fair_values: Annotated[dict[str, float] | None, Field(default=None, description="Optional: your fair YES probability per market_id, for sizing.")] = None,
        scenario: Annotated[str | None, Field(default=None, description="Optional what-if: 'market_id:yes|no' comma list to stress the book.")] = None,
    ) -> dict:
        result = deps.assess_portfolio(legs, bankroll_usd, fair_values)
        response = {
            "portfolio": result,
            "realtime": True,
            **deps.staleness(deps.now()),
            **_cost_note("assess_portfolio"),
        }
        if scenario:  # scenario stress test (K2)
            impact = deps.scenario_portfolio_impact(scenario, legs)
            if impact is not None:
                response["scenario_impact"] = impact
        return response

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


async def _sample_basis(ctx: "Context | None", a, b) -> dict | None:
    """Ask the client's model to judge settlement basis risk via MCP sampling (K3).

    Off unless RULEBOOK_SAMPLING is set and a sampling-capable client is attached.
    Any failure (no support, timeout, bad reply) returns None so the caller keeps
    its heuristic/operator-LLM assessment — the analysis is a bonus, never a
    dependency.
    """
    from core import rulesample

    if ctx is None or not rulesample.sampling_enabled():
        return None
    try:
        result = await ctx.sample(
            rulesample.build_user_message(a, b),
            system_prompt=rulesample.SYSTEM_PROMPT,
            max_tokens=400,
            temperature=0.0,
        )
    except Exception:
        return None  # client can't sample / declined / errored -> degrade
    return rulesample.parse(getattr(result, "text", None))


def _parse_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt
