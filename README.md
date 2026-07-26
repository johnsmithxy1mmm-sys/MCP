# predmarket-mcp

A **monetizable remote MCP server** that sells prediction-market intelligence
(Polymarket, Kalshi) as tools other AI agents call — and pay for — per call.
Not another bot: **rails**. Normalized data, mispricing detection, and honest
**realizable edge** (after fees/gas/slippage), packaged as tools an agent can
lean on instead of building itself.

The server is a **thin wrapper over a `core/` engine** (matcher, signals,
realizable-edge, storage). It returns **intelligence only** — it never executes
trades or holds funds.

> **Engine status.** Two interchangeable engines share one surface, selected by
> `CORE_ENGINE` (the MCP layer never changes either way):
> - **`mock`** (default) — realistic, same-signature stubs (`core/mock.py`) so
>   the server works end-to-end offline.
> - **`live`** — real Polymarket + Kalshi adapters (`core/adapters/`) feeding a
>   live engine (`core/live.py`). Needs network access to the venue APIs; falls
>   back gracefully (empty results) if a venue is unreachable.
>
> The shared intelligence (matcher, signals, realizable-edge) lives in
> `core/algorithms.py` and is used by **both** engines — not duplicated.

## Tool catalog (12 tools, 11 resources, 1 prompt)

Descriptions are the agent's only documentation, so they're written as copy.
Every response carries freshness (`as_of` / `data_age_seconds`) and cost
(`tier` / `price_usd`).

### Free tier (discovery + trust — the funnel)
| Tool | What it answers |
|---|---|
| `search_markets(query, category?, venue?)` | Discover markets by keyword. |
| `list_venues()` | Which venues exist, their status and coverage. |
| `track_record()` | **Verifiable performance** — how flagged opportunities actually did at resolution (hit-rate, edge slippage, Brier). The reason to trust the paid tools. |
| `evaluate_market(venue, market_id)` | Prices, implied prob, depth for one market. **Data delayed ~60s** on the free tier. |

### Paid tier (per-call revenue — realtime)
| Tool | What it answers | Price/call |
|---|---|---|
| `find_mispricing(min_edge, kind?, category?)` | Flagship. Live **cross_venue / bundle / dutch_book** opportunities above a **realizable** edge threshold; each risk-adjusted (annualized edge, holding days). Signed. | $0.05 |
| `compare_across_venues(event)` | Same event across venues: spread, direction, match confidence. | $0.02 |
| `estimate_execution(legs, size_usd)` | Realizable edge **at your size** from current depth, with per-venue cost breakdown. | $0.01 |
| `get_market_history(venue, market_id, from_ts, to_ts)` | Historical price/spread series. | $0.01 |
| `watch(min_edge, kind?, category?, event?)` | **Subscribe** to opportunities instead of polling — register once, get alerts. | $0.02 |
| `poll_alerts()` | Retrieve opportunities that fired against your watches (delivered once). | $0.005 |

Prices live in [`pricing.yaml`](./pricing.yaml), never hardcoded.

- **Resources:** `market://{venue}/{market_id}` (snapshot), `house://{venue}/{market_id}`
  (the server's own fused probability), `maker://{venue}/{market_id}` (market-maker
  quote advice), `scenario://scan/{spec}` (what-if stress test), `leaderboard://top`
  (public proof-of-alpha ranking), `portfolio://me` (your own paper P&L),
  `alerts://me` (your queued alerts), plus `event://`, `conditional://`,
  `distribution://`, `outcomes://` — for agents that prefer resources over tools.
- **Identity:** personal resources are addressed as `me` and resolved from a
  **derived** identity — an OAuth subject, or the payer address of a *verified*
  x402 payment. `x-client-id` is only a namespace hint inside an already-proven
  identity; it can never select a principal. There is deliberately no URI that
  names another caller, so cross-tenant reads are impossible by construction
  rather than by check. Set `REQUIRE_PROVEN_IDENTITY=on` to refuse personal
  resources to unauthenticated callers entirely.
- **Prompt:** `arbitrage_scan_workflow(min_edge)` — guides an agent scan →
  confirm → estimate execution → rank.

## Quick start

```bash
uv sync                                   # Python 3.12, deps
uv run pytest                             # 22 tests, all green
uv run python -m predmarket_mcp.server    # streamable-http on http://0.0.0.0:8000/mcp
curl -s http://127.0.0.1:8000/health      # {"status":"ok",...}

# live data from Polymarket + Kalshi (needs network egress to the venue APIs):
CORE_ENGINE=live uv run python -m predmarket_mcp.server
```

Verify with the official MCP Inspector (see [`tests/test_inspector.md`](./tests/test_inspector.md)):

```bash
npx @modelcontextprotocol/inspector       # UI → Streamable HTTP → http://127.0.0.1:8000/mcp
```

## Connecting from a client

**Direct HTTP agent** (Cursor, LangGraph, any MCP client that speaks Streamable HTTP):

```json
{
  "mcpServers": {
    "predmarket": { "url": "https://your-host/mcp", "transport": "streamable-http" }
  }
}
```

**stdio-only hosts (Claude Desktop / Claude Code)** — bridge to the remote server
with `mcp-remote`:

```json
{
  "mcpServers": {
    "predmarket": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "https://your-host/mcp"]
    }
  }
}
```

## Monetization

Two rails; **x402 is primary**, API-key/metering is the fallback. Both are inert
until you flip the flag — the server can take money, but doesn't gate at launch
(**usage first, billing later**).

```bash
PAID_ENABLED=false   # default: paid tools run free, metering still records usage
PAID_ENABLED=true    # enforce the gate on paid tools
PAYMENT_RAIL=x402    # or "apikey" for the OAuth/metering fallback
```

### x402 (agent-native, stablecoin micropayments)
A paid tool call without a signed `X-PAYMENT` header gets a real **HTTP 402**
with an x402 challenge (scheme, network, amount, pay-to). Retry with a valid
base64-JSON `X-PAYMENT` header → the `Facilitator` verifies it, a **receipt** is
logged, and the call is forwarded. Settlement in USDC.

> The default `MockFacilitator` does structural verification and stubs
> settlement (`# TODO: real facilitator/settlement`). The 402 flow, gating, and
> receipt log are real.

### Metering (fallback)
Every paid call writes **exactly one usage record** via a pluggable
`MeteringBackend`. Default is local **SQLite** (zero infra); `StripeBackend` /
`MoesifBackend` are typed stubs behind the same interface. OAuth 2.1 for the
API-key rail is wired via FastMCP helpers (`auth.py`), enabled by env.

### Configuration (all via env — no secrets in code)
| Var | Default | Purpose |
|---|---|---|
| `CORE_ENGINE` | `mock` | `mock` (offline stubs) or `live` (Polymarket/Kalshi adapters). Live mode needs outbound egress to the venue hosts (`gamma-api.polymarket.com`, `clob.polymarket.com`, `api.elections.kalshi.com`); if the network policy blocks them the live engine degrades gracefully to empty results and the per-venue circuit breaker opens (verified). |
| `PAID_ENABLED` | `false` | Master gate switch. |
| `PAYMENT_RAIL` | `x402` | `x402` or `apikey`. |
| `FREE_TIER_DELAY_SECONDS` | `60` | Free-tier data delay. |
| `METERING_BACKEND` | `local` | `local` \| `stripe` \| `moesif`. |
| `METERING_DB_URL` | `sqlite:///metering.db` | Usage/receipt store. |
| `HISTORY_DB_URL` | `sqlite:///history.db` | Price-history store. A `postgresql://` DSN engages the real asyncpg **Timescale/Postgres** path (degrades to SQLite if the driver/DB is unavailable). |
| `MATCHER` | `lexical` | Cross-venue matcher tier: `lexical` (offline), `semantic` (embeddings), `hybrid`. |
| `MATCH_MIN_CONFIDENCE` | `0.45` | Match threshold — tune when using `semantic`/`hybrid` (cosine is on a different scale). |
| `EMBED_BACKEND` / `EMBED_MODEL` | `fastembed` / `BAAI/bge-small-en-v1.5` | Embedder for the semantic tier. `EMBED_BACKEND=voyage` uses a hosted embedder (no model in the image — serverless-friendly). |
| `VOYAGE_API_KEY` | — | Required for `EMBED_BACKEND=voyage` (Voyage AI — Claude has no embeddings endpoint). |
| `RECON_DB_URL` | `sqlite:///reconciliation.db` | Track-record store (flagged → resolved → realized). |
| `WATCH_DB_URL` | `sqlite:///watches.db` | Watch/alert subscription store. |
| `SIGNING_KEY` | — | Operator secret; when set, responses carry an HMAC-SHA256 `provenance` signature. |
| `SIGNING_KEY_ED25519` | — | 32-byte Ed25519 seed (hex/base64). Preferred over HMAC — asymmetric signatures anyone can verify against the public key at `GET /pubkey`. |
| `X402_OPERATOR_WALLET` | — | Payee address for x402. |
| `X402_NETWORK` | `base-sepolia` | Settlement network. |
| `X402_FACILITATOR_URL` | — | **Set to enable real settlement**: a Coinbase/self-hosted x402 facilitator (`/verify` + `/settle`). Unset → the structural MockFacilitator. |
| `KALSHI_API_KEY_ID` / `KALSHI_PRIVATE_KEY` (or `_PATH`) | — | Live Kalshi private endpoints (orderbook) — RSA-PSS request signing. |
| `AUTH_JWKS_URI` / `AUTH_ISSUER` / `AUTH_AUDIENCE` | — | OAuth 2.1 fallback. |
| `HOST` / `PORT` | `0.0.0.0` / `8000` | Bind address. |
| **v2 — Provable Alpha Rails** | | *(all off/degrading by default)* |
| `STREAMING` | `off` | Real-time WSS ingestion of venue price ticks into the history store. Degrades to the TTL-fetch path if unset / no `websockets` / dropped connection. |
| `ALERT_ENGINE` / `ALERT_ENGINE_INTERVAL` | `off` / `30` | Background thread that fires watches off the hot path; `poll_alerts` becomes drain-only. Off → firing happens inline (synchronous, serverless-safe). |
| `ALERT_WEBHOOK_URL` / `ALERT_WEBHOOK_SECRET` | — | Push fired alerts to a webhook (best-effort; alert stays queued for `poll_alerts` on failure). |
| `WATCH_MAX_PER_CLIENT` | `50` | Per-client active-watch cap. |
| `ANALYST_ENABLED` / `ANTHROPIC_API_KEY` | `off` / — | LLM resolution-risk analyst (Claude `claude-opus-4-8`, structured tool output, adaptive thinking). Off/no-key → offline heuristic. `ANALYST_MODEL`, `ANALYST_MAX_ENRICH` (`3`) tune it. |
| `KELLY_FRACTION` | `0.5` | Fractional-Kelly multiplier for `estimate_execution` sizing (half-Kelly). |
| `FAIRVALUE_MIN_LIQUIDITY_USD` | `1.0` | Liquidity floor for the cross-venue consensus weighting. |
| `MANIFOLD_ENABLED` | `off` | Add Manifold as a third market/consensus venue (live engine). |
| `RATE_LIMIT_RPM` / `RATE_LIMIT_BURST` | `0` / `=RPM` | Per-client token-bucket rate limit (429 on exceed). `0` = disabled. |
| `CIRCUIT_FAIL_MAX` / `CIRCUIT_RESET_TIMEOUT` | `5` / `30` | Per-venue circuit breaker: open after N failures, half-open probe after cooldown. |
| `HISTORY_RETENTION_DAYS` | `90` | Prune history older than this (throttled). `0` = keep everything. |
| `LOG_FORMAT` / `LOG_LEVEL` | `text` / `INFO` | `LOG_FORMAT=json` for structured logs. |
| **v3 — Provable Alpha (the moat)** | | *(intelligence built on our own history/logic)* |
| `CALIBRATION_MIN_SAMPLES` / `CALIBRATION_BINS` | `30` / `10` | Online probability calibration from resolution history (identity until enough outcomes). |
| `ANALYST_ENABLED` / `ANTHROPIC_API_KEY` | `off` / — | Also powers the resolution-**basis** LLM diff (settlement-rule comparison); heuristic otherwise. |
| `RESOLUTION_ENGINE` / `RESOLUTION_INTERVAL` | `off` / `3600` | Autonomous resolver: settles pending flagged opportunities against venue resolution APIs so the track record / calibration / Merkle anchor grow with no operator (needs `CORE_ENGINE=live`). |
| `OPTIONS_ENABLED` / `DERIBIT_API_URL` | `off` / deribit.com | Options-implied probability cross-check (Breeden–Litzenberger) in `evaluate_market`. |
| `MICROSTRUCTURE_WINDOW` | `20` | Lookback for the informed-flow / momentum read. |
| `ANCHOR_ENABLED` / `ANCHOR_RPC_URL` / `ANCHOR_NETWORK` / `ANCHOR_MIN_INTERVAL` | `off` / — / `base-sepolia` / `3600` | On-chain anchoring of the track-record Merkle root (mock chain until an RPC is set). |
| `KELLY_FRACTION` (reuse) | `0.5` | Kelly can be fed the **calibrated** probability, not the raw price. |

**Entailment & term-structure arbitrage** (C1) and **value-based pricing** for
`get_market_history` (scales with range, capped) need no flags — always on.

| **v4 — autonomy & product depth** | | |
| `PERSISTENCE_MIN_SAMPLES` / `EXEC_HORIZON_SECONDS` | `20` / `60` | Edge-survival model: ranks `find_mispricing` by expected value (edge × P(survives the execution window)), learned from opportunity lifespans. |
| `QUALITY_GATE` / `QUALITY_MIN` | `off` / `0.5` | Quote-quality guard: skip scans on stale/thin/crossed quotes. Advisory (surfaced in `evaluate_market`) unless the gate is on. |
| `ADMIN_TOKEN` | — | Enables the operator-only `GET /revenue` (charged-vs-settled reconciliation); sent as `X-Admin-Token`. |
| `ALERT_WEBHOOK_SECRET` (reuse) | — | Now also HMAC-signs the webhook body (`X-Signature: sha256=…`). |

New surfaces: free `event://{entity}` resource (the whole market structure for a
subject: related markets, implies/same_event/term_neighbor relations, transitive
entailment violations, term structures); paid `assess_portfolio` tool (net
exposure, correlation, portfolio Kelly, hedges); `simulate_strategy` now includes
an out-of-sample walk-forward check.

**Quality & provable evidence** (J): every `find_mispricing` opportunity carries
an `adverse` block — a trap_score (informed flow against the position +
historical hit-rate of edges of that kind + quote quality) and a
`trust_adjusted_edge`. Pass `audit=true` for a signed **audit bundle** per
opportunity: inputs, prices, book depth and timestamp, Ed25519-signed and
fetchable at public `GET /audit/{id}` — a buyer independently verifies the edge
existed, without trusting our word (the deepest, least-copyable trust primitive).

**Capital-velocity strategy** (I): `find_mispricing` prices TIME, not just edge —
every opportunity carries `velocity` metrics (capital_efficiency = EV per locked
day, compound_annual_growth under recycling, early-exit liquidity from book
depth), `rank="velocity"` puts the fastest capital turnover first, and passing
`bankroll_usd` + `horizon_days` returns a **rotation_plan**: allocations across
short-cycle opportunities with a projected compounded bankroll as capital
recycles (`VELOCITY_DEFAULT_DAYS`, `ROTATION_MAX_POSITIONS`).

**Implied distribution & dependency graph** (H/J): free `distribution://{entity}`
resource reconstructs the market's whole implied distribution from a threshold
ladder (survival curve, percentiles, implied mean, prob at any strike — the
prediction-market vol surface) plus a **continuous lognormal fit** (J2:
P(X in [a,b]) for any range, moments, quantiles); free `conditional://{event}`
resource returns market-implied P(A|B) (Gaussian copula from co-movement + exact
logical overrides) plus **Bayesian belief updates** (J3: multi-hop P(A | B,C…) by
naive-Bayes log-odds). `compare_across_venues` adds a **calibration-weighted
meta_consensus** (J4): a best-estimate + credible interval that weights each venue
by how accurate it has historically been (Brier), learned as markets resolve —
not just liquidity. The cross-venue matcher **learns from
resolution ground truth** — `compare_across_venues` reports a history-calibrated
match confidence, tuned by which past matches actually resolved the same way.

**House probability, scenarios & client-sampled rulebooks** (K): the server
publishes its OWN forecast, not just the market's. `evaluate_market` and the free
`house://{venue}/{market_id}` resource return a **`house_probability`** (K1) that
fuses calibration, options-implied, accuracy-weighted consensus and microstructure
into one number with a confidence and its `edge_vs_market`; every forecast is
recorded and, when the market resolves, **Brier-scored** — `track_record`'s
`house_forecast` block reports `brier_edge` (>0 means the house beats the raw
market), a claim no competitor can fake. The free `scenario://scan/{spec}` resource
(K2, e.g. `scenario://scan/pm-btc-150k-2026:yes`) pins markets to a hypothetical
outcome and propagates the consequences across the entity graph — prior → posterior
→ shift per related market (logical edges exact, copula edges estimated); pass
`scenario=` to `assess_portfolio` to **stress a book** under the what-if. And with
`RULEBOOK_SAMPLING=on`, `compare_across_venues` uses **MCP sampling** (K3) to ask
the *calling agent's own model* to judge settlement basis risk — the deepest
rulebook analysis, run at the client's expense; degrades to the heuristic when the
client can't sample.

**Agent leaderboard — proof-of-alpha as a service** (K4): an agent doesn't just
find edge here, it can *prove* it. Call `estimate_execution(commit=true)` and the
position is logged as a paper trade tagged to you; when the markets resolve it's
P&L-scored by the same model as the track record. The free, server-signed
`leaderboard://top` resource ranks agents by REAL, resolution-verified return
(handles anonymized; a single lucky trade stays provisional until
`LEADERBOARD_MIN_RESOLVED`), and the caller-scoped `portfolio://me`
resource shows your own trades, rank and realized P&L. The server becomes an
independent notary of an agent's performance — a portable, tamper-evident record
to show a third party, and the network effect the product was missing.

**Market-maker advisor** (K5): the other side of the book. Every other tool serves
a taker (lift someone's mispricing); the free `maker://{venue}/{market_id}` resource
serves a maker — where to post a two-sided limit quote to *earn* the spread
(`recommended_bid`/`ask`, `half_spread`, `skew`) and a `pull_quotes` guard that says
step aside when the flow looks informed. It prices adverse-selection risk from
microstructure (C8) + quote quality (F7), widening and skewing quotes against
informed flow. Intelligence only — it never posts orders.

**Reality feeds — the market vs the world** (K7): every other signal is
intramarket; K7 adds an EXTERNAL anchor. With `REALITY_ENABLED=on` and a
`REALITY_NOWCAST_URL` feed (a poll aggregator, a macro nowcast, an internal model),
`evaluate_market` reports a `reality` block — the market's probability vs the
real-world nowcast and their divergence ("market says 60%, the data says 45%") —
and the reality-implied probability feeds the house forecast (K1) as an
independent, heavily-weighted component. The divergence math is pure/offline; the
feed is a flag-gated provider (like the Deribit options cross-check) that degrades
to nothing when unconfigured.

**Native multi-outcome markets** (G): free `outcomes://{event}` resource
reconstructs an N-outcome event (election, bracketed number) from its binary
markets — **de-vigged** fair probabilities (margin removed so they sum to 1), the
overround, the favorite, and a **completeness-aware** full dutch book (an
over-round set is always arbitrage; an under-round is only free money if the
outcome set is complete — flagging otherwise is a false arb the naive path emits).
Adapters populate `event_group` from venue grouping (Polymarket negRisk, Kalshi
event ticker).

| **Horizontal scaling** | | *(single-instance defaults need nothing)* |
| `REDIS_URL` | — | Shared backend for the x402 replay guard (atomic `SET NX`) and rate limiter (global fixed-window). **Required for correctness behind a load balancer** — without it each instance has its own replay set, so one signed payment could buy a call per instance. Degrades to local SQLite/memory if unset or `redis` isn't installed. |
| `NONCE_TTL_SECONDS` | `2592000` | TTL for consumed-payment fingerprints in Redis (30d). |
| `PG_POOL_MIN` / `PG_POOL_MAX` | `1` / `10` | asyncpg pool bounds for the Postgres history path. |

Multi-instance deploy: point every instance at the same `REDIS_URL` (replay +
rate limits) and a `postgresql://` `HISTORY_DB_URL` (shared time series). The
async asyncpg path runs on a dedicated loop thread so the sync stores are
unchanged. Install the extras: `--extra redis --extra postgres`.

## Preflight: validate the live path before launch

Every automated test runs against **mocks** — the venue adapters normalize a
response shape assumed from docs. Real APIs differ, and that gap is the single
biggest launch risk. `deploy/preflight.py` is the check the test suite
structurally cannot do: it calls the real venues, pushes what comes back through
the actual adapters and the whole pipeline (normalization → matching → claim
parsing → scan → execution estimate), and names the link that breaks.

```bash
docker compose exec server python deploy/preflight.py          # all venues
docker compose exec server python deploy/preflight.py kalshi   # one venue
docker compose exec server python deploy/preflight.py --json   # for monitoring
```

Read-only (writes to no store), exits non-zero when the live path is broken, and
also audits production config — unsigned provenance, `PAID_ENABLED=true` with no
real facilitator (accepting payments that never settle), a missing
`RESOLUTION_ENGINE` (nothing ever resolves, so calibration / house Brier /
leaderboard stay empty forever). **Run it before serving traffic.**

## Deploy

```bash
docker build -t predmarket-mcp .
docker run -p 8000:8000 -e PAID_ENABLED=false predmarket-mcp

# with optional features baked in (extras + pre-downloaded embedding weights):
docker build -t predmarket-mcp \
  --build-arg EXTRAS="--extra semantic --extra kalshi --extra postgres" \
  --build-arg WITH_SEMANTIC=true .
```

Runs on Cloud Run / Container Apps / any container host. Streamable HTTP is
serverless-compatible. Terminate TLS and rate-limit at the proxy; use `/health`
for liveness. The container starts via `python -m predmarket_mcp.server` so the
x402 ASGI middleware is wired in (equivalent to `fastmcp run` + payment gating).
`WITH_SEMANTIC=true` pre-fetches the bge-small weights into the image so the
semantic matcher runs with no Hugging Face egress at runtime.

## Layout

```
src/predmarket_mcp/
  server.py     FastMCP app; /health, /metrics, /pubkey, /track-record; HTTP app + middleware
  tools.py      the 11 tools (call core/, format for agents — no logic here)
  resources.py  market:// · house:// · maker:// · scenario:// · leaderboard:// · portfolio://me · alerts://me · event:// · outcomes:// · distribution:// · conditional:// resources
  identity.py   caller identity DERIVED from OAuth / verified x402 payer, never from a header
  multioutcome.py native N-outcome markets: de-vig + complete-set dutch book (G)
  distribution.py implied distribution from a threshold ladder — vol surface (H1)
  conditional.py  market-implied P(A|B): Gaussian copula + logical overrides (H2)
  matchlearn.py   matcher calibrated from resolution ground truth (H5)
  prompts.py    arbitrage_scan_workflow
  config.py     env-driven settings (PAID_ENABLED flag)
  deps.py       the ONLY seam into core/
  provenance.py signed provenance (Ed25519 preferred, HMAC fallback)
  ops.py        metrics registry, token-bucket rate limit, structured logging
  auth.py       OAuth 2.1 fallback wiring
  billing/      tiers.py · metering.py · x402.py · middleware.py
core/
  models.py     canonical pydantic models
  algorithms.py shared matcher / signals / realizable-edge + risk finalize (mock + live reuse)
  entailment.py logical-implication + probability term-structure arbitrage (C1)
  basis.py      resolution-criteria diff / settlement basis risk (C2)
  calibration.py isotonic online probability calibration from history (C3)
  backtest.py   mean-reversion backtester over recorded history (C4)
  options.py    Breeden-Litzenberger options-implied probability, Deribit (C7)
  microstructure.py informed-flow / momentum read from the tape (C8)
  anchor.py     on-chain anchoring of the track-record Merkle root (C6)
  fairvalue.py  liquidity-weighted cross-venue consensus + deviation (B3)
  sizing.py     fractional-Kelly stake + market-impact curve (B4)
  analyst.py    LLM resolution-risk + basis analyst + offline heuristic (B5/C2)
  merkle.py     Merkle commitments/proofs for the track record (B6)
  circuit.py    per-venue circuit breaker (B7)
  streaming.py  WSS price ingestion (B1) · notifier.py  webhook alert push (B2)
  embeddings.py Embedder backends for the semantic matcher tier (fastembed default)
  mock.py       realistic offline engine (default)
  live.py       live engine: adapters + algorithms, TTL-cached, history ingest
  storage.py    price-history store (SQLite default; asyncpg Timescale/PG via DSN)
  pg.py         asyncpg loop-thread bridge for the sync stores (D2)
  reconciliation.py  flag → resolve → realized-edge / hit-rate / Brier + Merkle root
  houseview.py  fuse calibration+options+consensus+microstructure -> house probability (K1)
  houseforecast.py record + Brier-grade the server's own forecasts vs the market (K1)
  scenario.py   what-if propagation across the market graph + portfolio stress (K2)
  rulesample.py client-sampled (MCP sampling) settlement-basis analysis (K3)
  leaderboard.py agent proof-of-alpha: paper trades, P&L, public signed ranking (K4)
  maker.py      market-maker quote advisor + adverse-selection guard (K5)
  reality.py    external nowcast anchor + market-vs-world divergence (K7)
  watches.py    watch/alert store + background AlertEngine (push computed, pull drained)
  adapters/     base.py · polymarket.py · kalshi.py · manifold.py (fetch + normalize only)
tests/          40+ offline tests: tools · billing · adapters · storage · matcher · hardening · streaming · push · fairvalue · sizing · analyst · trust · ops · manifold
```

## Design principles honored
- **≤ 15 tools** (7 here) — agent tool-selection degrades past ~25–30.
- Tools are shaped around **agent questions**, not 1:1 API endpoints.
- **Realizable** edge, never gross. Every response marks data staleness.
- **No custody, no auto-execution** — intelligence only.
- `core/` logic is **not duplicated** — tools call the engine.
- Secrets via **env only**.

## Note on FastMCP version
The spec referenced "FastMCP 3.x"; this builds on the current
[`fastmcp`](https://pypi.org/project/fastmcp/) **3.x** (decorator API, Streamable
HTTP, OAuth helpers). SSE is intentionally unused (deprecated).
