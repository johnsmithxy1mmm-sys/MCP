#!/usr/bin/env python
"""Live-path preflight — validate the server against REAL venue APIs before launch.

Every automated test in this repo runs against mocks: the adapters normalize data
whose shape we ASSUMED from venue docs. Real responses differ — renamed fields,
different pagination, auth requirements, empty categories. This script is the one
check the test suite structurally cannot do: it calls the live venues, pushes what
comes back through the actual adapters and the whole intelligence pipeline, and
reports exactly which link breaks.

Run it on the machine that has egress (your VPS), BEFORE flipping traffic on:

    docker compose exec server python deploy/preflight.py          # all venues
    docker compose exec server python deploy/preflight.py kalshi   # one venue
    docker compose exec server python deploy/preflight.py --json   # machine-readable

Exit code 0 = the live path works end to end; 1 = something is broken (details in
the report). Nothing is written to any store: this is read-only reconnaissance.
"""

from __future__ import annotations

import json
import os
import sys
import time
import traceback

# The engine must be live for this to mean anything — force it before importing
# anything that reads the flag.
os.environ["CORE_ENGINE"] = "live"

OK, WARN, FAIL = "PASS", "WARN", "FAIL"


class Report:
    """Collects per-check results and renders a verdict."""

    def __init__(self):
        self.rows: list[dict] = []

    def add(self, check: str, status: str, detail: str = "", data=None) -> None:
        self.rows.append({"check": check, "status": status, "detail": detail, "data": data})
        if not self._json_mode():
            icon = {OK: "\033[32m✓\033[0m", WARN: "\033[33m!\033[0m", FAIL: "\033[31m✗\033[0m"}[status]
            print(f"  {icon} {check}: {detail}")

    @staticmethod
    def _json_mode() -> bool:
        return "--json" in sys.argv

    @property
    def failed(self) -> bool:
        return any(r["status"] == FAIL for r in self.rows)

    def render(self) -> int:
        if self._json_mode():
            print(json.dumps({"rows": self.rows, "ok": not self.failed}, indent=2, default=str))
            return 1 if self.failed else 0
        n_fail = sum(1 for r in self.rows if r["status"] == FAIL)
        n_warn = sum(1 for r in self.rows if r["status"] == WARN)
        print("\n" + "=" * 62)
        if self.failed:
            print(f"VERDICT: NOT READY — {n_fail} failed, {n_warn} warnings.")
            print("Fix the ✗ items before serving traffic. Each one means the live")
            print("path returns nothing (or wrong data) where a caller expects value.")
        elif n_warn:
            print(f"VERDICT: USABLE with {n_warn} warning(s). Review the ! items —")
            print("they degrade quality (thin coverage, missing depth) but don't break.")
        else:
            print("VERDICT: READY. The live path works end to end against real venues.")
        print("=" * 62)
        return 1 if self.failed else 0


def _section(title: str) -> None:
    if "--json" not in sys.argv:
        print(f"\n\033[1m{title}\033[0m")


# --- 1. venue reachability + market normalization ---------------------------
def check_venue(rep: Report, name: str, adapter) -> list:
    """Fetch markets from one venue and sanity-check what the adapter produced."""
    _section(f"[{name}] connectivity + normalization")
    t0 = time.monotonic()
    try:
        markets = adapter.fetch_markets(limit=50)
    except Exception as exc:
        rep.add(f"{name} fetch_markets", FAIL,
                f"{type(exc).__name__}: {exc}. Check egress/DNS, the API base URL, "
                f"and (Kalshi) credentials.")
        return []
    ms = round((time.monotonic() - t0) * 1000)

    if not markets:
        rep.add(f"{name} fetch_markets", FAIL,
                f"reachable in {ms}ms but returned 0 usable markets — the response "
                f"shape likely changed; inspect the raw JSON and update the adapter's "
                f"_normalize_market().")
        return []
    rep.add(f"{name} fetch_markets", OK, f"{len(markets)} markets in {ms}ms")

    # Prices must be normalized probabilities, not cents/percent.
    bad = [m for m in markets if not (0.0 <= m.yes_price <= 1.0)]
    if bad:
        rep.add(f"{name} price normalization", FAIL,
                f"{len(bad)} markets outside [0,1] (e.g. {bad[0].market_id}="
                f"{bad[0].yes_price}) — a unit conversion is wrong.")
    else:
        rep.add(f"{name} price normalization", OK, "all prices within [0,1]")

    # A YES+NO book that doesn't sum near 1 means the NO side is derived wrongly.
    skew = [m for m in markets if abs((m.yes_price + m.no_price) - 1.0) > 0.15]
    if len(skew) > len(markets) * 0.5:
        rep.add(f"{name} yes/no coherence", WARN,
                f"{len(skew)}/{len(markets)} markets have yes+no far from 1.0 — "
                f"either real spread, or the NO price is mis-derived.")
    else:
        rep.add(f"{name} yes/no coherence", OK, "yes+no ≈ 1 on most markets")

    # Titles feed the matcher and every parser; empty titles kill the pipeline.
    empty = [m for m in markets if not (m.title or "").strip()]
    if empty:
        rep.add(f"{name} titles", FAIL,
                f"{len(empty)} markets have no title — matching, entailment and "
                f"clustering will all silently produce nothing.")
    else:
        rep.add(f"{name} titles", OK, "all markets carry a title")

    # Volume drives sizing and fair-value weighting.
    no_vol = [m for m in markets if not m.volume_usd]
    if len(no_vol) == len(markets):
        rep.add(f"{name} volume", WARN,
                "no volume on any market — size caps and liquidity weighting "
                "will fall back to defaults.")
    else:
        rep.add(f"{name} volume", OK, f"{len(markets) - len(no_vol)}/{len(markets)} have volume")

    # Close time drives holding period -> annualized edge and velocity.
    no_close = [m for m in markets if m.close_time is None]
    if len(no_close) == len(markets):
        rep.add(f"{name} close_time", WARN,
                "no close times — holding period falls back to the default, so "
                "annualized edge and capital velocity are estimates only.")
    else:
        rep.add(f"{name} close_time", OK, f"{len(markets) - len(no_close)}/{len(markets)} dated")

    # event_group is what makes dutch-book / multi-outcome detection possible.
    grouped = [m for m in markets if m.event_group]
    if not grouped:
        rep.add(f"{name} event_group", WARN,
                "no grouped markets in this sample — dutch-book and outcomes:// "
                "will find nothing from this venue.")
    else:
        rep.add(f"{name} event_group", OK, f"{len(grouped)} markets carry a group id")

    return markets


# --- 2. orderbook depth (the paid tools depend on it) -----------------------
def check_orderbook(rep: Report, name: str, adapter, markets: list) -> None:
    _section(f"[{name}] orderbook depth")
    if not markets:
        rep.add(f"{name} orderbook", FAIL, "skipped — no markets to probe")
        return
    probe = max(markets, key=lambda m: m.volume_usd or 0)
    try:
        book = adapter.fetch_orderbook(probe.market_id)
    except Exception as exc:
        rep.add(f"{name} fetch_orderbook", FAIL,
                f"{type(exc).__name__}: {exc} on {probe.market_id}. Private endpoint? "
                f"(Kalshi needs KALSHI_API_KEY_ID + RSA key.)")
        return
    if book is None:
        rep.add(f"{name} fetch_orderbook", FAIL,
                f"returned None for {probe.market_id} — estimate_execution, "
                f"find_mispricing size caps and maker:// all degrade to nothing.")
        return
    if not book.yes_asks and not book.yes_bids:
        rep.add(f"{name} fetch_orderbook", FAIL,
                f"empty book for {probe.market_id} (the venue's most liquid market) "
                f"— depth parsing is likely wrong.")
        return
    top_bid, top_ask = book.top_bid_price, book.top_ask_price
    rep.add(f"{name} fetch_orderbook", OK,
            f"{len(book.yes_bids)} bids / {len(book.yes_asks)} asks on {probe.market_id}")
    if top_bid is not None and top_ask is not None:
        if top_bid >= top_ask:
            rep.add(f"{name} book sanity", FAIL,
                    f"crossed book: bid {top_bid} >= ask {top_ask} — bid/ask sides "
                    f"are probably swapped in the adapter.")
        else:
            rep.add(f"{name} book sanity", OK,
                    f"bid {top_bid} < ask {top_ask}, spread {round(top_ask - top_bid, 4)}")
    depth = sum(l.size_usd for l in book.yes_asks)
    if depth <= 0:
        rep.add(f"{name} depth sizes", FAIL, "all level sizes are 0 — size units mis-parsed.")
    else:
        rep.add(f"{name} depth sizes", OK, f"~${round(depth)} on the ask side")


# --- 3. the intelligence pipeline on real data ------------------------------
def check_pipeline(rep: Report, markets: list) -> None:
    """The part that actually earns money: matching -> signals -> edge."""
    _section("[pipeline] intelligence on live data")
    if len(markets) < 2:
        rep.add("pipeline", FAIL, "skipped — need markets from at least one venue")
        return

    from core import algorithms
    from core.entailment import parse_claim

    # Cross-venue matching is the flagship's precondition.
    try:
        pairs = algorithms.match_markets(markets)
    except Exception as exc:
        rep.add("matcher", FAIL, f"{type(exc).__name__}: {exc}")
        return
    venues = {m.venue.value for m in markets}
    if len(venues) < 2:
        rep.add("matcher", WARN,
                f"only {venues} returned data — cross-venue arbitrage needs two live "
                f"venues; fix the missing one or expect cross_venue to stay empty.")
    elif not pairs:
        rep.add("matcher", WARN,
                "0 cross-venue matches in this sample. Normal for a 50-market slice; "
                "if it persists with full coverage, lower MATCH_MIN_CONFIDENCE.")
    else:
        best = pairs[0]
        rep.add("matcher", OK,
                f"{len(pairs)} pairs, best {best.confidence}: "
                f"'{best.a.title[:38]}' ↔ '{best.b.title[:38]}'")

    # The claim parser powers entailment, clustering, distribution, scenarios.
    parsed = [m for m in markets if parse_claim(m) is not None]
    if not parsed:
        rep.add("claim parser", WARN,
                "0 titles parsed into threshold claims — entailment arb, "
                "distribution:// and scenario:// will be empty for this venue's "
                "title style.")
    else:
        rep.add("claim parser", OK, f"{len(parsed)}/{len(markets)} titles parsed")

    # Full scan through the real engine.
    try:
        from core import live

        t0 = time.monotonic()
        opps = live.scan_opportunities(0.0)
        ms = round((time.monotonic() - t0) * 1000)
    except Exception as exc:
        rep.add("scan_opportunities", FAIL,
                f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=3)}")
        return
    rep.add("scan_opportunities", OK, f"{len(opps)} raw opportunities in {ms}ms")
    if ms > 15000:
        rep.add("scan latency", WARN,
                f"{ms}ms per full scan — a paid call would feel slow; consider "
                f"raising LIVE_MARKET_TTL or narrowing venue limits.")

    # Edge sanity: the number the whole product sells.
    real = [o for o in opps if o.realizable_edge >= 0.01]
    if opps:
        top = max(opps, key=lambda o: o.realizable_edge)
        if top.realizable_edge > 0.5:
            rep.add("edge sanity", WARN,
                    f"top edge {top.realizable_edge:.1%} looks too good — usually a "
                    f"false match or a mis-parsed price. Inspect: {top.title[:50]}")
        else:
            rep.add("edge sanity", OK,
                    f"top realizable edge {top.realizable_edge:.2%} ({top.kind.value})")
    rep.add("actionable opportunities", OK if real else WARN,
            f"{len(real)} above 1% — " + ("real inventory to sell" if real else
            "none right now; normal in efficient conditions, but verify over a day"))

    # Execution estimate on the best opportunity — the money math end to end.
    if opps:
        try:
            est = live.realizable_edge(top.legs, 500.0)
            rep.add("estimate_execution", OK,
                    f"$500 basket -> fillable ${est.fillable_size_usd}, "
                    f"net edge {est.realizable_edge:.2%}, fees ${est.fees_usd}")
        except Exception as exc:
            rep.add("estimate_execution", FAIL, f"{type(exc).__name__}: {exc}")


# --- 4. config sanity (what will bite you in production) --------------------
def check_config(rep: Report) -> None:
    _section("[config] production readiness")
    from predmarket_mcp.provenance import public_key_info

    if public_key_info() is None:
        rep.add("signing key", WARN,
                "SIGNING_KEY_ED25519 unset — /pubkey returns 404 and provenance is "
                "unsigned. The trust story is the product; set it.")
    else:
        rep.add("signing key", OK, "Ed25519 configured; /pubkey will verify")

    if os.getenv("PAID_ENABLED", "").lower() in ("1", "true", "yes", "on"):
        if not os.getenv("X402_FACILITATOR_URL"):
            rep.add("x402 settlement", FAIL,
                    "PAID_ENABLED=true with no X402_FACILITATOR_URL — the mock "
                    "facilitator accepts payments that never settle. You would be "
                    "giving away paid calls.")
        elif not os.getenv("X402_OPERATOR_WALLET"):
            rep.add("x402 wallet", FAIL, "PAID_ENABLED=true but no X402_OPERATOR_WALLET.")
        else:
            rep.add("x402 settlement", OK, "facilitator + wallet configured")
    else:
        rep.add("x402 gate", OK, "PAID_ENABLED=false (usage-first launch)")

    if not os.getenv("ADMIN_TOKEN"):
        rep.add("admin token", WARN, "ADMIN_TOKEN unset — /revenue reconciliation disabled")
    else:
        rep.add("admin token", OK, "/revenue protected")

    if os.getenv("RESOLUTION_ENGINE", "").lower() in ("1", "on", "true", "yes"):
        rep.add("resolution engine", OK,
                "on — the track record, calibration and house Brier grow autonomously")
    else:
        rep.add("resolution engine", WARN,
                "RESOLUTION_ENGINE unset — nothing will ever resolve, so calibration, "
                "survival, house Brier and the leaderboard stay empty forever. This is "
                "the moat; turn it on.")


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    only = args[0].lower() if args else None
    rep = Report()

    if "--json" not in sys.argv:
        print("\033[1mpredmarket-mcp preflight — live venue validation\033[0m")
        print("Read-only. Nothing is recorded to any store.")

    from core.adapters import KalshiAdapter, PolymarketAdapter

    venues = {"polymarket": PolymarketAdapter(), "kalshi": KalshiAdapter()}
    if os.getenv("MANIFOLD_ENABLED", "").lower() in ("1", "on", "true", "yes"):
        from core.adapters import ManifoldAdapter

        venues["manifold"] = ManifoldAdapter()

    all_markets: list = []
    for name, adapter in venues.items():
        if only and name != only:
            continue
        markets = check_venue(rep, name, adapter)
        check_orderbook(rep, name, adapter, markets)
        all_markets.extend(markets)

    check_pipeline(rep, all_markets)
    check_config(rep)
    return rep.render()


if __name__ == "__main__":
    sys.exit(main())
