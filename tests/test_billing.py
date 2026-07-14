"""Billing tests: metering (one record per paid call) and the x402 402 flow."""

from __future__ import annotations

import json

import pytest

from predmarket_mcp.billing.metering import LocalBackend
from predmarket_mcp.billing.middleware import (
    BillingContext,
    X402Middleware,
    get_billing,
)
from predmarket_mcp.billing.x402 import encode_payment_header
from predmarket_mcp.config import Settings


# --- metering ---------------------------------------------------------------
@pytest.mark.asyncio
async def test_paid_call_writes_exactly_one_usage_record(client):
    billing = get_billing()
    before = billing.metering.count()
    await client.call_tool("find_mispricing", {"min_edge": 0.02})
    assert billing.metering.count() == before + 1


@pytest.mark.asyncio
async def test_free_call_is_not_metered(client):
    billing = get_billing()
    before = billing.metering.count()
    await client.call_tool("search_markets", {"query": "fed"})
    assert billing.metering.count() == before


@pytest.mark.asyncio
async def test_usage_record_fields(client):
    billing = get_billing()
    await client.call_tool("compare_across_venues", {"event": "bitcoin 100k"})
    rec = billing.metering.records()[-1]
    assert rec["tool_name"] == "compare_across_venues"
    assert rec["price_usd"] == 0.02
    assert rec["status"] == "ok"
    assert rec["currency"] == "USDC"


# --- x402: paid gate on --------------------------------------------------------
def _paid_billing(tmp_path) -> BillingContext:
    """A billing context with the gate ON and an isolated DB."""
    settings = Settings(
        paid_enabled=True,
        payment_rail="x402",
        metering_db_url=f"sqlite:///{tmp_path / 'paid.db'}",
    )
    return BillingContext(settings)


async def _run_asgi(mw, body: bytes, headers: list) -> tuple[int, bytes]:
    scope = {"type": "http", "method": "POST", "path": "/mcp", "headers": headers}
    sent = []
    # The transport yields the body once, then http.disconnect on every later
    # receive() — exactly like a real ASGI server after the request body.
    events = [{"type": "http.request", "body": body, "more_body": False}]

    async def receive():
        if events:
            return events.pop(0)
        return {"type": "http.disconnect"}

    async def send(m):
        sent.append(m)

    await mw(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    body_msg = next((m for m in sent if m["type"] == "http.response.body"), {"body": b""})
    return start["status"], body_msg["body"]


def _tool_call_body(name: str, args: dict) -> bytes:
    return json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": name, "arguments": args}}
    ).encode()


async def _ok_downstream(scope, receive, send):
    # Read the full body via receive (proves the middleware replays it), then
    # do one more receive expecting http.disconnect (the real handler does this;
    # a broken replay that emits synthetic http.request messages would hang or
    # never deliver the disconnect).
    body = b""
    while True:
        msg = await receive()
        if msg["type"] == "http.request":
            body += msg.get("body", b"")
            if not msg.get("more_body", False):
                break
        else:
            break
    disconnect = await receive()
    assert disconnect["type"] == "http.disconnect", "replay must delegate to real receive"
    await send({"type": "http.response.start", "status": 200,
                "headers": [(b"content-type", b"application/json")]})
    await send({"type": "http.response.body", "body": b'{"ok":true}'})


@pytest.mark.asyncio
async def test_paid_tool_without_payment_returns_402(tmp_path):
    billing = _paid_billing(tmp_path)
    mw = X402Middleware(_ok_downstream, billing)
    status, body = await _run_asgi(mw, _tool_call_body("find_mispricing", {"min_edge": 0.02}), [])
    assert status == 402
    challenge = json.loads(body)
    assert challenge["error"] == "payment_required"
    assert challenge["accepts"][0]["amount_usd"] == 0.05
    assert billing.receipts.count() == 0


@pytest.mark.asyncio
async def test_paid_tool_with_valid_payment_passes_and_logs_receipt(tmp_path):
    billing = _paid_billing(tmp_path)
    mw = X402Middleware(_ok_downstream, billing)
    header = encode_payment_header({
        "scheme": "exact",
        "network": billing.settings.x402_network,
        "payload": {"signature": "0xsig",
                    "authorization": {"from": "0xpayer", "to": "0xop", "value": "50000"}},
    })
    status, body = await _run_asgi(
        mw, _tool_call_body("find_mispricing", {"min_edge": 0.02}),
        [(b"x-payment", header.encode())],
    )
    assert status == 200
    assert billing.receipts.count() == 1


@pytest.mark.asyncio
async def test_underpayment_is_rejected(tmp_path):
    billing = _paid_billing(tmp_path)
    mw = X402Middleware(_ok_downstream, billing)
    header = encode_payment_header({
        "scheme": "exact",
        "network": billing.settings.x402_network,
        "payload": {"signature": "0xsig",
                    "authorization": {"from": "0xp", "to": "0xo", "value": "1"}},
    })
    status, body = await _run_asgi(
        mw, _tool_call_body("find_mispricing", {"min_edge": 0.02}),
        [(b"x-payment", header.encode())],
    )
    assert status == 402
    assert "insufficient" in json.loads(body).get("error_detail", "")


@pytest.mark.asyncio
async def test_free_tool_bypasses_gate(tmp_path):
    billing = _paid_billing(tmp_path)
    mw = X402Middleware(_ok_downstream, billing)
    status, _ = await _run_asgi(mw, _tool_call_body("search_markets", {"query": "x"}), [])
    assert status == 200  # free tools never blocked


@pytest.mark.asyncio
async def test_gate_off_lets_paid_tool_through(tmp_path):
    """Default (PAID_ENABLED=false): paid tools run without payment."""
    settings = Settings(paid_enabled=False, metering_db_url=f"sqlite:///{tmp_path / 'off.db'}")
    billing = BillingContext(settings)
    mw = X402Middleware(_ok_downstream, billing)
    status, _ = await _run_asgi(mw, _tool_call_body("find_mispricing", {"min_edge": 0.02}), [])
    assert status == 200


# --- pricing sanity ---------------------------------------------------------
def test_pricing_tiers_match_spec():
    from predmarket_mcp.billing.tiers import load_pricing

    pricing = load_pricing()
    assert pricing.get("search_markets").tier == "free"
    assert pricing.get("evaluate_market").price_usd == 0
    assert pricing.get("find_mispricing").is_paid
    assert pricing.get("find_mispricing").price_usd == 0.05
