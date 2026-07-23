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


def event_view(entity: str) -> dict:
    """Event-graph view for an entity: related markets across venues + their
    logical relations + transitive entailment violations (F3)."""
    from core.eventgraph import event_view as _view

    markets = repo.search_markets(entity, category=None, venue=None)
    return _view(entity, markets)


def conditional_view(event: str, limit: int = 6) -> dict:
    """Market-implied conditional probabilities P(A|B) among related markets:
    Gaussian copula (correlation from history) with logical overrides (H2)."""
    from core.conditional import pairwise
    from core.eventgraph import build_clusters

    markets = repo.search_markets(event, category=None, venue=None)
    markets = sorted(markets, key=lambda m: m.volume_usd or 0, reverse=True)[:limit]
    if len(markets) < 2:
        return {"event": event, "found": False, "conditionals": []}
    # Exact overrides: entailment (P(weak|strong)=1) and mutual exclusion (=0).
    logical: dict[tuple[str, str], float] = {}
    for cluster in build_clusters(markets).values():
        for rel in cluster.relations:
            if rel["type"] == "implies":
                logical[(rel["from"], rel["to"])] = 1.0  # P(weak | strong) = 1
    by_group: dict[str, list[str]] = {}
    for m in markets:
        if m.event_group:
            by_group.setdefault(m.event_group, []).append(m.market_id)
    for ids in by_group.values():
        for x in ids:
            for y in ids:
                if x != y:
                    logical[(x, y)] = 0.0  # mutually exclusive -> P(x | y) = 0

    from datetime import timedelta

    def hist(venue, market_id):
        end = now()
        return get_history(venue, market_id, end - timedelta(days=30), end)

    return {"event": event, "found": True,
            "conditionals": pairwise(markets, hist, logical)}


def distribution_view(entity: str) -> dict:
    """Implied probability distribution for a numeric event from its threshold
    ladder: survival curve, percentiles, tail probs, implied mean (H1)."""
    from core.distribution import view as _view

    markets = repo.search_markets(entity, category=None, venue=None)
    return _view(entity, markets)


def outcome_view(event: str) -> dict:
    """Native multi-outcome view for an event group: de-vigged probabilities,
    overround, favorite, and a completeness-aware full dutch book (G)."""
    from core.multioutcome import build_from_markets, view as _mom_view

    markets = repo.search_markets(event, category=None, venue=None)
    moms = build_from_markets(markets)
    q = event.strip().lower()
    matched = [m for m in moms if q in m.event.lower() or
               any(q in (o.name or "").lower() for o in m.outcomes)] or moms
    if not matched:
        return {"event": event, "found": False, "multi_outcome_markets": []}
    return {"event": event, "found": True,
            "multi_outcome_markets": [_mom_view(m) for m in matched]}


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


def assess_basis(a: Market, b: Market) -> dict:
    """Settlement basis risk: do these two markets resolve by the same rules? (C2)"""
    from core.basis import assess_basis as _assess

    return _assess(a, b)


def calibrate_probability(p: float, category: str | None = None) -> dict:
    """History-calibrated probability for a raw price (C3). Identity until enough
    resolved outcomes exist."""
    from core.calibration import calibrate_probability as _cal

    return _cal(p, category)


def backtest_strategy(
    venue: str, market_id: str, frm: datetime, to: datetime, params
) -> dict:
    """Replay a mean-reversion rule over recorded history with honest costs (C4)
    plus an out-of-sample walk-forward check (F6)."""
    from core.backtest import run

    points = get_history(venue, market_id, frm, to)
    return run(points, params)


def quote_quality(yes_price, volume_usd, data_age_seconds=None) -> dict:
    """Reliability score + flags for a quote (F7)."""
    from core.quality import quote_quality as _q

    return _q(yes_price, volume_usd, data_age_seconds)


def microstructure(venue: str, market_id: str) -> dict | None:
    """Informed-flow microstructure read from recent price history (C8)."""
    from datetime import timedelta

    from core.microstructure import compute

    end = now()
    points = get_history(venue, market_id, end - timedelta(days=7), end)
    return compute(points)


_CRYPTO_CCY = {"bitcoin": "BTC", "btc": "BTC", "ethereum": "ETH", "eth": "ETH"}


def options_divergence(market: Market) -> dict | None:
    """Options-implied vs market probability for a crypto strike market (C7).

    None unless OPTIONS_ENABLED and the title parses to a supported crypto strike.
    """
    from core.entailment import parse_claim
    from core.options import divergence, get_provider

    provider = get_provider()
    if provider is None:
        return None
    claim = parse_claim(market)
    if claim is None or claim.direction != "above":
        return None
    currency = next((c for tok, c in _CRYPTO_CCY.items() if tok in claim.entity), None)
    if currency is None:
        return None
    implied = provider.implied_probability(currency, claim.threshold)
    if implied is None:
        return None
    return {**divergence(market.yes_price, implied), "currency": currency,
            "strike": claim.threshold}


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


def assess_portfolio(
    legs: list[Leg], bankroll_usd: float | None = None,
    fair_values: dict | None = None,
) -> dict:
    """Portfolio-level exposure, correlation, sizing and hedges (F4)."""
    from core.portfolio import assess

    return assess(
        legs, get_market, get_history,
        related_getter=lambda entity: repo.search_markets(entity, category=None, venue=None),
        bankroll=bankroll_usd, fair_values=fair_values,
    )


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


def anchor_track_record() -> dict | None:
    """Anchor the current Merkle root on-chain if enabled + due (C6). Returns the
    latest anchor, or None when ANCHOR_ENABLED is unset."""
    from core.anchor import anchor_track_record as _anchor

    return _anchor()


def resolve_outcomes(outcomes: dict[str, int]) -> int:
    n = get_reconciliation().resolve(outcomes)
    # Also teach the matcher: matched pairs that resolved the same were true
    # matches; ones that diverged weren't (H5).
    try:
        from core.matchlearn import get_matchlearn

        get_matchlearn().resolve(outcomes)
    except Exception:
        pass
    return n


def record_match(a_id: str, b_id: str, confidence: float) -> None:
    """Note a surfaced cross-venue match so it can be scored at resolution (H5)."""
    from core.matchlearn import get_matchlearn

    get_matchlearn().record(a_id, b_id, confidence)


def match_confidence(confidence: float) -> dict:
    """History-calibrated true-match probability for a raw similarity (H5)."""
    from core.matchlearn import get_matchlearn

    return get_matchlearn().calibrate(confidence)


# --- watches / alerts (push subscription model) ----------------------------
from core.watches import (  # noqa: E402
    WatchLimitError,
    alert_engine_running,
    get_watches,
    start_alert_engine,
)


def _scan_all() -> list[Opportunity]:
    """Full unfiltered scan — the firing input for watches/the alert engine.
    Also the survival-tracking observation point (full set is required)."""
    opps = scan_opportunities(0.0, None, None)
    try:
        from core.persistence import get_persistence

        get_persistence().observe(opps)  # learn opportunity lifespans over time
    except Exception:
        pass  # survival tracking is best-effort, never breaks a scan
    return opps


def rank_by_expected_value(opps: list[Opportunity], observe_full: bool) -> list[Opportunity]:
    """Attach survival_probability + expected_value and rank by EV (F2).

    When the scan was unfiltered we also feed the survival model, so lifespans
    keep accruing even if no watches/alert engine are running.
    """
    from core.persistence import get_persistence
    from core.velocity import annotate_velocity

    store = get_persistence()
    if observe_full:
        try:
            store.observe(opps)
        except Exception:
            pass
    ranked = store.annotate(opps)
    # Attach capital-velocity metrics (holding, efficiency, compound growth).
    # Book-free here — early-exit liquidity is added only on the velocity path.
    return annotate_velocity(ranked)


def rank_by_velocity(opps: list[Opportunity]) -> list[Opportunity]:
    """Re-rank by capital velocity (EV per locked day) and enrich the shortlist
    with early-exit liquidity from live books (I1)."""
    from core.velocity import annotate_early_exit, rank_by_velocity as _rank

    ranked = _rank(opps)
    return annotate_early_exit(ranked, get_orderbook)


def rotation_plan(opps: list[Opportunity], bankroll_usd: float,
                  horizon_days: float = 30.0) -> dict:
    """Bankroll rotation plan across short-cycle opportunities (I1)."""
    from core.velocity import rotation_plan as _plan

    return _plan(opps, bankroll_usd, horizon_days)


# If ALERT_ENGINE is set, a background thread does the firing (poll becomes
# drain-only). Off by default: firing happens inline in create_watch/poll_alerts
# so offline tests and serverless deploys stay fully synchronous.
start_alert_engine(_scan_all)

# If RESOLUTION_ENGINE is set, a background thread settles pending flagged
# opportunities against venue resolution APIs — so the track record, calibration
# and Merkle anchor grow autonomously. Off by default (needs live venues).
from core.resolution import start_resolution_engine  # noqa: E402

start_resolution_engine()


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
