"""Thin accessors to the core engine and storage.

This is the ONLY place the MCP layer reaches into ``core``. Swapping the mock
for the real engine happens here (and in ``core/mock.py``), not in the tools.
Tools call these helpers and format the result for agents — no business logic
lives above this line.
"""

from __future__ import annotations

from datetime import datetime, timezone

# Engine selection: mock (default, offline) or live (real Polymarket/Kalshi
# adapters). Both expose the same surface, so this is the only switch needed —
# nothing in tools.py / the MCP layer changes.
#   CORE_ENGINE=mock  (default)
#   CORE_ENGINE=live  -> core.live (requires network access to venue APIs)
import os as _os

if _os.getenv("CORE_ENGINE", "mock").lower() == "live":
    from core import live as _engine
else:
    from core import mock as _engine

from core.models import (
    ExecutionEstimate,
    Leg,
    Market,
    MatchedPair,
    OrderbookSnapshot,
    Opportunity,
    PricePoint,
)

repo = _engine.repo


# --- Engine passthroughs ----------------------------------------------------
def search_markets(query: str, category: str | None, venue: str | None) -> list[Market]:
    return repo.search_markets(query, category=category, venue=venue)


def list_venues() -> list[dict]:
    return repo.list_venues()


def get_market(venue: str, market_id: str) -> Market | None:
    return repo.get_market(venue, market_id)


def get_orderbook(venue: str, market_id: str) -> OrderbookSnapshot | None:
    return repo.get_orderbook(venue, market_id)


def get_history(
    venue: str, market_id: str, frm: datetime, to: datetime
) -> list[PricePoint]:
    return repo.get_history(venue, market_id, frm, to)


def match_event(event: str) -> MatchedPair | None:
    return _engine.match_event(event)


def assess_fair_value(markets: list[Market]) -> dict:
    """Cross-venue consensus fair value + per-venue deviation for a set of quotes."""
    from core.fairvalue import assess

    return assess(markets)


def scan_opportunities(
    min_edge: float, kind: str | None, category: str | None
) -> list[Opportunity]:
    return _engine.scan_opportunities(min_edge, kind=kind, category=category)


def enrich_resolution_risk(opps: list[Opportunity]) -> list[Opportunity]:
    """Replace the default resolution_risk on the top opportunities with the LLM
    analyst's structured score (B5). No-op unless ANALYST_ENABLED + a key are set;
    capped to ANALYST_MAX_ENRICH to bound cost/latency."""
    import os as _o

    from core.analyst import get_analyst

    analyst = get_analyst()
    if analyst is None:
        return opps  # offline default: heuristic 0.02 already set by annotate_risk
    cap = int(_o.getenv("ANALYST_MAX_ENRICH", "3"))
    for opp in opps[:cap]:
        if not opp.legs:
            continue
        primary = get_market(opp.legs[0].venue.value, opp.legs[0].market_id)
        if primary is None:
            continue
        try:
            opp.resolution_risk = analyst.score(primary)["resolution_risk"]
        except Exception:
            pass  # enrichment must never break the scan
    return opps


def realizable_edge(legs: list[Leg], size_usd: float) -> ExecutionEstimate:
    return _engine.realizable_edge(legs, size_usd)


def execution_sizing(
    legs: list[Leg], size_usd: float,
    bankroll_usd: float | None = None, fair_value: float | None = None,
) -> dict:
    """Kelly stake + market-impact curve for a position (B4)."""
    from core.sizing import execution_sizing as _sizing

    return _sizing(legs, size_usd, repo.get_orderbook, bankroll_usd, fair_value)


# --- reconciliation / track record -----------------------------------------
from core.reconciliation import get_reconciliation  # noqa: E402


def record_flagged(opps: list[Opportunity]) -> None:
    """Best-effort: record flagged opportunities for later reconciliation."""
    store = get_reconciliation()
    for opp in opps:
        prices: dict[str, float] = {}
        for leg in opp.legs:
            m = get_market(leg.venue.value, leg.market_id)
            if m is not None:
                prices[leg.market_id] = m.yes_price
        try:
            store.record(opp, prices)
        except Exception:
            pass  # recording must never break the tool


def track_record_metrics() -> dict:
    return get_reconciliation().metrics()


def track_record_commitment() -> dict:
    """Merkle root committing to the resolved track record (tamper-evidence)."""
    return get_reconciliation().merkle_commitment()


def resolve_outcomes(outcomes: dict[str, int]) -> int:
    return get_reconciliation().resolve(outcomes)


# --- watches / alerts (push subscription model) ----------------------------
from core.watches import (  # noqa: E402
    WatchLimitError,
    alert_engine_running,
    get_watches,
    start_alert_engine,
)


def _scan_all() -> list[Opportunity]:
    """Full unfiltered scan — the firing input for watches/the alert engine."""
    return scan_opportunities(0.0, None, None)


# If ALERT_ENGINE is set, a background thread does the firing (poll becomes
# drain-only). Off by default: firing happens inline in create_watch/poll_alerts
# so offline tests and serverless deploys stay fully synchronous.
start_alert_engine(_scan_all)


def create_watch(client_id, min_edge, category=None, kind=None, event=None) -> dict:
    """Register a watch, run an immediate scan, return id + any instant matches."""
    store = get_watches()
    try:
        watch_id = store.create_watch(client_id, min_edge, category, kind, event)
    except WatchLimitError as exc:
        return {"error": "watch_limit_reached", "detail": str(exc)}
    store.fire(_scan_all())  # evaluate against current opps (immediate matches)
    immediate = [a for a in store.drain(client_id) if a["watch_id"] == watch_id]
    return {"watch_id": watch_id, "immediate_matches": immediate}


def poll_alerts(client_id) -> list[dict]:
    """Drain this client's queued alerts. Fires inline only when no background
    engine is running (otherwise the engine already did the scan)."""
    store = get_watches()
    if not alert_engine_running():
        store.fire(_scan_all())
    return store.drain(client_id)


def list_watches(client_id) -> list[dict]:
    return get_watches().list_watches(client_id)


def peek_alerts(client_id) -> list[dict]:
    """Undelivered alerts WITHOUT draining (backs the alerts:// resource)."""
    return get_watches().peek(client_id)


# --- Staleness helpers ------------------------------------------------------
def now() -> datetime:
    return datetime.now(timezone.utc)


def staleness(as_of: datetime) -> dict:
    """Standard freshness marker attached to every tool response."""
    age = (now() - as_of).total_seconds()
    return {"as_of": as_of.isoformat(), "data_age_seconds": round(max(0.0, age), 1)}
