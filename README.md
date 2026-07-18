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

## Tool catalog (11 tools, 2 resources, 1 prompt)

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

- **Resource:** `market://{venue}/{market_id}` — market snapshot for agents that
  prefer resources over tool calls.
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
| `OPTIONS_ENABLED` / `DERIBIT_API_URL` | `off` / deribit.com | Options-implied probability cross-check (Breeden–Litzenberger) in `evaluate_market`. |
| `MICROSTRUCTURE_WINDOW` | `20` | Lookback for the informed-flow / momentum read. |
| `ANCHOR_ENABLED` / `ANCHOR_RPC_URL` / `ANCHOR_NETWORK` / `ANCHOR_MIN_INTERVAL` | `off` / — / `base-sepolia` / `3600` | On-chain anchoring of the track-record Merkle root (mock chain until an RPC is set). |
| `KELLY_FRACTION` (reuse) | `0.5` | Kelly can be fed the **calibrated** probability, not the raw price. |

**Entailment & term-structure arbitrage** (C1) and **value-based pricing** for
`get_market_history` (scales with range, capped) need no flags — always on.

| **Horizontal scaling** | | *(single-instance defaults need nothing)* |
| `REDIS_URL` | — | Shared backend for the x402 replay guard (atomic `SET NX`) and rate limiter (global fixed-window). **Required for correctness behind a load balancer** — without it each instance has its own replay set, so one signed payment could buy a call per instance. Degrades to local SQLite/memory if unset or `redis` isn't installed. |
| `NONCE_TTL_SECONDS` | `2592000` | TTL for consumed-payment fingerprints in Redis (30d). |
| `PG_POOL_MIN` / `PG_POOL_MAX` | `1` / `10` | asyncpg pool bounds for the Postgres history path. |

Multi-instance deploy: point every instance at the same `REDIS_URL` (replay +
rate limits) and a `postgresql://` `HISTORY_DB_URL` (shared time series). The
async asyncpg path runs on a dedicated loop thread so the sync stores are
unchanged. Install the extras: `--extra redis --extra postgres`.

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
  resources.py  market:// + alerts:// resources
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
