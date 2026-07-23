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


def _valid_header(billing, value="50000", to="0xop"):
    return encode_payment_header({
        "scheme": "exact", "network": billing.settings.x402_network,
        "payload": {"signature": "0xsig",
                    "authorization": {"from": "0xpayer", "to": to, "value": value}},
    })


def _batch_body(*names_args) -> bytes:
    return json.dumps([
        {"jsonrpc": "2.0", "id": i, "method": "tools/call",
         "params": {"name": n, "arguments": a}}
        for i, (n, a) in enumerate(names_args)
    ]).encode()


@pytest.mark.asyncio
async def test_replayed_payment_is_rejected(tmp_path):
    """A1: the same signed X-PAYMENT can be used exactly once."""
    billing = _paid_billing(tmp_path)
    mw = X402Middleware(_ok_downstream, billing)
    header = _valid_header(billing)
    body = _tool_call_body("find_mispricing", {"min_edge": 0.02})
    first, _ = await _run_asgi(mw, body, [(b"x-payment", header.encode())])
    second, resp = await _run_asgi(mw, body, [(b"x-payment", header.encode())])
    assert first == 200
    assert second == 402
    assert json.loads(resp).get("error_detail") == "payment_reused"
    assert billing.receipts.count() == 1  # only the first settled


@pytest.mark.asyncio
async def test_batch_must_pay_the_sum(tmp_path):
    """A2: N paid calls in one request need one payment covering their sum."""
    billing = _paid_billing(tmp_path)
    mw = X402Middleware(_ok_downstream, billing)
    # find_mispricing ($0.05) + compare_across_venues ($0.02) = $0.07 = 70000 atomic.
    batch = _batch_body(
        ("find_mispricing", {"min_edge": 0.02}),
        ("compare_across_venues", {"event": "btc"}),
    )
    # Paying only for one tool ($0.05) is rejected as underpayment.
    under, ubody = await _run_asgi(mw, batch, [(b"x-payment", _valid_header(billing, "50000").encode())])
    assert under == 402 and "insufficient" in json.loads(ubody).get("error_detail", "")
    # Paying the full sum passes.
    ok, _ = await _run_asgi(mw, batch, [(b"x-payment", _valid_header(billing, "70000").encode())])
    assert ok == 200


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


# --- privacy (A3) -----------------------------------------------------------
def test_client_fingerprint_never_stores_raw_secret():
    from predmarket_mcp.billing.middleware import client_fingerprint

    assert client_fingerprint({"x-client-id": "acme"}) == "acme"
    fp = client_fingerprint({"authorization": "Bearer super-secret"})
    assert fp.startswith("auth-") and "super-secret" not in fp
    assert client_fingerprint({}) is None


@pytest.mark.asyncio
async def test_metering_stores_param_shape_not_values(client):
    import json as _json
    from predmarket_mcp.billing.middleware import get_billing

    billing = get_billing()
    before = billing.metering.count()
    await client.call_tool("find_mispricing", {"min_edge": 0.02})
    rec = billing.metering.records()[-1]
    assert billing.metering.count() == before + 1
    params = _json.loads(rec["params"])
    assert params == {"keys": ["min_edge"], "count": 1}  # shape only, no value 0.02


# --- pricing sanity ---------------------------------------------------------
def test_pricing_tiers_match_spec():
    from predmarket_mcp.billing.tiers import load_pricing

    pricing = load_pricing()
    assert pricing.get("search_markets").tier == "free"
    assert pricing.get("evaluate_market").price_usd == 0
    assert pricing.get("find_mispricing").is_paid
    assert pricing.get("find_mispricing").price_usd == 0.05


# --- body-size cap (memory-DoS guard) ---------------------------------------
@pytest.mark.asyncio
async def test_oversized_body_rejected_with_413(tmp_path, monkeypatch):
    import predmarket_mcp.billing.middleware as mwmod

    monkeypatch.setattr(mwmod, "MAX_BODY_BYTES", 1024)
    billing = _paid_billing(tmp_path)
    mw = X402Middleware(_ok_downstream, billing)
    status, body = await _run_asgi(mw, b"x" * 2048, [])
    assert status == 413
    assert json.loads(body)["error"] == "payload_too_large"


@pytest.mark.asyncio
async def test_normal_body_passes_under_the_cap(tmp_path):
    billing = _paid_billing(tmp_path)
    mw = X402Middleware(_ok_downstream, billing)
    # A free-tool call well under the cap flows through untouched.
    status, _ = await _run_asgi(mw, _tool_call_body("search_markets", {"query": "fed"}), [])
    assert status == 200


# --- nonce store TTL prune --------------------------------------------------
def test_nonce_store_prunes_expired_fingerprints(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from predmarket_mcp.billing.x402 import NonceStore

    monkeypatch.setenv("NONCE_TTL_SECONDS", "3600")
    store = NonceStore(f"sqlite:///{tmp_path / 'n.db'}", redis_client=None)
    # Seed one expired fingerprint directly, then trigger a prune via mark_used.
    old_ts = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    with store._connect() as conn:
        conn.execute("INSERT INTO used_payments (fingerprint, ts) VALUES (?, ?)",
                     ("stale-fp", old_ts))
    assert store.mark_used("fresh-fp") is True
    assert store.is_used("stale-fp") is False  # swept
    assert store.is_used("fresh-fp") is True   # kept
