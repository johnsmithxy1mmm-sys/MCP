# MCP Inspector checklist

Manual/scripted verification of the server through the official MCP Inspector.
All steps below have been run against this build and pass.

## Start the server

```bash
uv run python -m predmarket_mcp.server        # streamable-http on :8000/mcp
# health:
curl -s http://127.0.0.1:8000/health          # -> {"status":"ok",...}
```

## Interactive Inspector

```bash
npx @modelcontextprotocol/inspector           # opens the UI; connect to:
#   Transport: Streamable HTTP
#   URL:       http://127.0.0.1:8000/mcp
```

## Non-interactive (CLI) — quick regression

```bash
BASE=http://127.0.0.1:8000/mcp
INSP="npx -y @modelcontextprotocol/inspector --cli $BASE --transport http"

$INSP --method tools/list
$INSP --method tools/call --tool-name search_markets   --tool-arg query=bitcoin
$INSP --method tools/call --tool-name list_venues
$INSP --method tools/call --tool-name evaluate_market   --tool-arg venue=polymarket --tool-arg market_id=pm-btc-100k-2026
$INSP --method tools/call --tool-name find_mispricing   --tool-arg min_edge=0.02
$INSP --method tools/call --tool-name compare_across_venues --tool-arg event="bitcoin above 100k end of 2026"
$INSP --method resources/templates/list
$INSP --method prompts/list
$INSP --method prompts/get  --prompt-name arbitrage_scan_workflow --prompt-args min_edge=0.03
```

## Checklist

- [x] `tools/list` returns exactly 7 tools; all `inputSchema` valid JSON Schema.
- [x] Tool descriptions read as user-facing copy; args use plain names
      (`event`, `min_edge`, `venue`).
- [x] Every tool result carries `tier`, `price_usd`, `as_of`, `data_age_seconds`.
- [x] Free tools (`search_markets`, `list_venues`, `evaluate_market`) return
      `tier: free`, `price_usd: 0`.
- [x] `evaluate_market` returns `realtime: false` and `data_age_seconds ≈ 60`
      (free-tier delay).
- [x] Paid tools return `realtime: true` and `tier: paid` with a non-zero price.
- [x] `find_mispricing` opportunities all have `realizable_edge ≥ min_edge`
      (edge is net of fees/gas/slippage, never gross).
- [x] `resources/templates/list` shows `market://{venue}/{market_id}`; a read
      returns a snapshot.
- [x] `prompts/list` shows `arbitrage_scan_workflow`; `prompts/get` renders and
      reflects the `min_edge` argument.
- [x] `/health` returns HTTP 200.
- [x] Catalog token weight is modest (~1.2k tokens, well under 15k).

## Payment gate (x402) — with `PAID_ENABLED=true`

The 402 flow is enforced at the ASGI layer (real HTTP 402). Covered by
`tests/test_billing.py` (`test_paid_tool_without_payment_returns_402`,
`test_paid_tool_with_valid_payment_passes_and_logs_receipt`):

- [x] Paid `tools/call` with no `X-PAYMENT` header → HTTP 402 + x402 challenge.
- [x] Paid `tools/call` with a valid signed `X-PAYMENT` header → 200; receipt logged.
- [x] Underpayment / wrong network / missing signature → 402 with `error_detail`.
- [x] Free tools bypass the gate.
- [x] With `PAID_ENABLED=false` (default) paid tools run without payment.
```
