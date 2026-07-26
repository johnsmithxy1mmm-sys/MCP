"""Billing middleware wiring: metering + x402 enforcement + flag gate.

Two layers, each with a single responsibility:

* ``MeteringMiddleware`` (FastMCP tool middleware) — records EXACTLY ONE usage
  record per paid tool call, with latency/status and how it was paid. Runs on
  every transport, independent of enforcement.
* ``X402Middleware`` (raw ASGI) — enforces payment on the wire: a paid tool
  call without a valid ``X-PAYMENT`` header gets a real HTTP ``402`` with the
  x402 challenge; a valid signed header is verified, a receipt is logged, and
  the call is forwarded.

Both are inert unless ``PAID_ENABLED=true`` — the server is *able* to charge
but does not gate at launch (usage first, billing later).
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from functools import lru_cache

import anyio
from starlette.middleware import Middleware as ASGIMiddleware

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_headers
from fastmcp.server.middleware import Middleware, MiddlewareContext

from ..config import Settings, get_settings
from .metering import UsageRecord, build_backend
from .tiers import Pricing, load_pricing
from .x402 import (
    Facilitator,
    NonceStore,
    PaymentError,
    PaymentRequirement,
    ReceiptStore,
    build_facilitator,
    decode_payment_header,
    payment_fingerprint,
)

PAYMENT_HEADER = "x-payment"

# Payment gating buffers the request body to inspect the JSON-RPC calls; cap it
# so an oversized POST can't balloon memory. Normal MCP payloads are a few KB.
MAX_BODY_BYTES = int(os.getenv("X402_MAX_BODY_BYTES", str(2 * 1024 * 1024)))


def client_fingerprint(headers: dict[str, str]) -> str | None:
    """Stable, privacy-preserving caller id — never the raw secret.

    x-client-id verbatim; otherwise a hash of the Authorization header (so the
    bearer token is never persisted); else None.
    """
    cid = headers.get("x-client-id")
    if cid:
        return cid
    auth = headers.get("authorization")
    if auth:
        return "auth-" + hashlib.sha256(auth.encode()).hexdigest()[:16]
    return None


# --- shared, process-wide billing state -------------------------------------
class BillingContext:
    """Holds pricing, facilitator, metering + receipt stores. Built once."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.pricing: Pricing = load_pricing()
        self.metering = build_backend(settings)
        self.receipts = ReceiptStore(settings.metering_db_url)
        self.nonces = NonceStore(settings.metering_db_url)
        self.facilitator: Facilitator = build_facilitator(settings)

    def revenue_summary(self) -> dict:
        """Reconcile what we CHARGED (metered paid calls) against what SETTLED
        (receipts) — business-level 'money not lost/doubled'."""
        try:
            usage = self.metering.records()
        except Exception:
            usage = []
        paid = [u for u in usage if u.get("paid_via")]
        charged = round(sum(float(u.get("price_usd") or 0) for u in paid), 6)
        receipts = self.receipts.all()
        settled = round(sum(float(r.get("amount_usd") or 0) for r in receipts), 6)
        return {
            "charged_usd": charged,
            "charged_calls": len(paid),
            "settled_usd": settled,
            "settled_receipts": len(receipts),
            "discrepancy_usd": round(charged - settled, 6),
            "currency": self.pricing.currency,
        }

    # -- gating helpers ----------------------------------------------------
    def gate_active(self) -> bool:
        return self.settings.paid_enabled and self.settings.payment_rail == "x402"

    def is_paid(self, tool_name: str) -> bool:
        return self.pricing.is_paid(tool_name)

    def requirement(self, calls: list[tuple[str, dict]]) -> PaymentRequirement:
        """Payment requirement for one paid call — or a batch (sum of prices).

        Each call is ``(tool_name, arguments)``; the amount is value-based
        (``price_for``) so e.g. a wider history query costs more. The total is the
        sum across the batch, so N calls need one payment covering all N.
        """
        names = [name for name, _ in calls]
        total = sum(self.pricing.price_for(name, args) for name, args in calls)
        return PaymentRequirement(
            tool_name="+".join(names),
            price_usd=round(total, 6),
            currency=self.pricing.currency,
            network=self.settings.x402_network,
            pay_to=self.settings.operator_wallet,
        )

    def check_x402(self, calls: list[tuple[str, dict]], headers: dict[str, str]) -> "PaymentDecision":
        """Verify payment for paid tool call(s). Replay-safe; one receipt on success.

        A batch of N paid calls requires ONE payment covering the SUM of their
        (value-based) prices. A previously-consumed signed payment is rejected.
        """
        requirement = self.requirement(calls)
        raw = headers.get(PAYMENT_HEADER)
        if not raw:
            return PaymentDecision(ok=False, challenge=requirement.to_challenge())
        try:
            payment = decode_payment_header(raw)
            fingerprint = payment_fingerprint(payment)
            # CLAIM the fingerprint atomically BEFORE settling. Settlement moves
            # real USDC and cannot be undone, so the claim must come first. The
            # previous order (is_used -> settle -> mark_used) let two concurrent
            # requests both pass the check and both reach the facilitator,
            # charging the payer twice for one served call while the books
            # recorded a single receipt — invisible to /revenue (audit INV-001).
            #
            # The claim is deliberately NOT released when settlement fails: a
            # failed settlement moved no money, so the caller loses nothing by
            # presenting a freshly signed payment, whereas releasing would
            # re-open the double-settle window whenever a facilitator error is
            # ambiguous about whether the transfer actually went through.
            if not self.nonces.mark_used(fingerprint):
                raise PaymentError("payment_reused")
            receipt = self.facilitator.verify_and_settle(payment, requirement)
        except PaymentError as exc:
            challenge = requirement.to_challenge()
            challenge["error_detail"] = str(exc)
            return PaymentDecision(ok=False, challenge=challenge)
        self.receipts.save(receipt)
        return PaymentDecision(ok=True, challenge=None, receipt_id=receipt.receipt_id)


@dataclass
class PaymentDecision:
    ok: bool
    challenge: dict | None
    receipt_id: str | None = None


@lru_cache(maxsize=1)
def get_billing() -> BillingContext:
    return BillingContext(get_settings())


# --- FastMCP tool middleware: exactly one usage record per paid call ---------
class MeteringMiddleware(Middleware):
    def __init__(self, billing: BillingContext):
        self.billing = billing

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        tool_name = context.message.name
        if not self.billing.is_paid(tool_name):
            return await call_next(context)  # free tools are not metered

        headers = get_http_headers(include_all=True) or {}
        paid_via = None
        if self.billing.gate_active() and headers.get(PAYMENT_HEADER):
            paid_via = "x402"
        elif self.billing.settings.paid_enabled and self.billing.settings.payment_rail == "apikey":
            paid_via = "apikey"

        client_id = client_fingerprint(headers)  # hashed — never the raw secret
        args = dict(context.message.arguments or {})
        # Charge the value-based amount (matches what the x402 gate enforces).
        charged = self.billing.pricing.price_for(tool_name, args)
        # Privacy: never persist raw argument VALUES (they can carry anything);
        # keep only the shape — enough for usage analytics and billing disputes.
        params_meta = {"keys": sorted(args.keys()), "count": len(args)}
        start = time.perf_counter()
        status = "ok"
        try:
            result = await call_next(context)
            return result
        except Exception:
            status = "error"
            raise
        finally:
            latency_ms = round((time.perf_counter() - start) * 1000, 2)
            try:
                from ..ops import METRICS

                METRICS.observe("tool_latency_ms", latency_ms)
                METRICS.inc("paid_calls_total")
            except Exception:
                pass
            usage = UsageRecord(
                tool_name=tool_name,
                price_usd=charged,
                currency=self.billing.pricing.currency,
                status=status,
                latency_ms=latency_ms,
                client_id=client_id,
                paid_via=paid_via,
                params=params_meta,
            )
            # SQLite write off the event loop.
            await anyio.to_thread.run_sync(self.billing.metering.record, usage)


# --- raw ASGI middleware: real HTTP 402 enforcement -------------------------
class X402Middleware:
    def __init__(self, app, billing: BillingContext, mcp_path: str = "/mcp"):
        self.app = app
        self.billing = billing
        self.mcp_path = mcp_path.rstrip("/")

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or scope.get("method") != "POST":
            return await self.app(scope, receive, send)
        if not self.billing.gate_active():
            return await self.app(scope, receive, send)
        if not scope.get("path", "").rstrip("/").endswith(self.mcp_path):
            return await self.app(scope, receive, send)

        body, messages = await _buffer_body(receive)
        if body is None:  # over the size cap — reject before parsing anything
            return await _send_413(send)
        calls = _paid_tool_calls(body, self.billing)
        if not calls:
            return await self.app(scope, _replay(messages, receive), send)

        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        # Payment verification does SQLite + (possibly) facilitator HTTP —
        # run it off the event loop.
        decision = await anyio.to_thread.run_sync(
            self.billing.check_x402, calls, headers
        )
        if not decision.ok:
            return await _send_402(send, decision.challenge)
        return await self.app(scope, _replay(messages, receive), send)


async def _buffer_body(receive):
    """Buffer the request body (returns ``(None, messages)`` when over the cap)."""
    body = b""
    messages = []
    more = True
    while more:
        message = await receive()
        messages.append(message)
        if message["type"] == "http.request":
            body += message.get("body", b"")
            if len(body) > MAX_BODY_BYTES:
                return None, messages
            more = message.get("more_body", False)
        else:
            more = False
    return body, messages


def _replay(messages, original_receive):
    """Replay the buffered request messages, then delegate to the real transport.

    Falling back to ``original_receive`` (instead of emitting synthetic
    ``http.request`` messages) is essential: the streamable-HTTP handler keeps
    calling ``receive()`` after the body to detect client disconnect. Feeding it
    fake messages makes it hang; delegating lets ``http.disconnect`` flow.
    """
    queue = list(messages)

    async def receive():
        if queue:
            return queue.pop(0)
        return await original_receive()

    return receive


def _paid_tool_calls(body: bytes, billing: BillingContext) -> list[tuple[str, dict]]:
    """Every paid (tool_name, arguments) in the request (a JSON-RPC batch may hold
    several). Returning ALL of them — with their args, for value-based pricing —
    is what stops a batch from paying for one call and getting the rest free.
    """
    try:
        payload = json.loads(body or b"{}")
    except json.JSONDecodeError:
        return []
    entries = payload if isinstance(payload, list) else [payload]
    calls: list[tuple[str, dict]] = []
    for call in entries:
        if not isinstance(call, dict) or call.get("method") != "tools/call":
            continue
        params = call.get("params") or {}
        name = params.get("name")
        if name and billing.is_paid(name):
            args = params.get("arguments") or {}
            calls.append((name, args if isinstance(args, dict) else {}))
    return calls


async def _send_413(send):
    body = json.dumps({"error": "payload_too_large",
                       "detail": f"request body exceeds {MAX_BODY_BYTES} bytes"}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


async def _send_402(send, challenge: dict):
    body = json.dumps(challenge).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 402,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"x-payment-required", b"1"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


# --- installation -----------------------------------------------------------
def install_billing_middleware(mcp: FastMCP) -> None:
    """Attach the metering middleware (all transports)."""
    mcp.add_middleware(MeteringMiddleware(get_billing()))


def asgi_middleware(mcp_path: str = "/mcp") -> list[ASGIMiddleware]:
    """ASGI middleware list for HTTP transport (real 402 enforcement)."""
    return [ASGIMiddleware(X402Middleware, billing=get_billing(), mcp_path=mcp_path)]
