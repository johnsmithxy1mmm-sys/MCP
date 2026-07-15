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

import json
import time
from dataclasses import dataclass
from functools import lru_cache

from starlette.middleware import Middleware as ASGIMiddleware

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_headers
from fastmcp.server.middleware import Middleware, MiddlewareContext

from ..config import Settings, get_settings
from .metering import UsageRecord, build_backend
from .tiers import Pricing, load_pricing
from .x402 import (
    Facilitator,
    PaymentError,
    PaymentRequirement,
    ReceiptStore,
    build_facilitator,
    decode_payment_header,
)

PAYMENT_HEADER = "x-payment"


# --- shared, process-wide billing state -------------------------------------
class BillingContext:
    """Holds pricing, facilitator, metering + receipt stores. Built once."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.pricing: Pricing = load_pricing()
        self.metering = build_backend(settings)
        self.receipts = ReceiptStore(settings.metering_db_url)
        self.facilitator: Facilitator = build_facilitator(settings)

    # -- gating helpers ----------------------------------------------------
    def gate_active(self) -> bool:
        return self.settings.paid_enabled and self.settings.payment_rail == "x402"

    def is_paid(self, tool_name: str) -> bool:
        return self.pricing.is_paid(tool_name)

    def requirement(self, tool_name: str) -> PaymentRequirement:
        p = self.pricing.get(tool_name)
        return PaymentRequirement(
            tool_name=tool_name,
            price_usd=p.price_usd,
            currency=self.pricing.currency,
            network=self.settings.x402_network,
            pay_to=self.settings.operator_wallet,
        )

    def check_x402(self, tool_name: str, headers: dict[str, str]) -> "PaymentDecision":
        """Verify payment for a paid tool. Saves a receipt on success."""
        requirement = self.requirement(tool_name)
        raw = headers.get(PAYMENT_HEADER)
        if not raw:
            return PaymentDecision(ok=False, challenge=requirement.to_challenge())
        try:
            payment = decode_payment_header(raw)
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

        pricing = self.billing.pricing.get(tool_name)
        client_id = headers.get("x-client-id") or headers.get("authorization")
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
            self.billing.metering.record(
                UsageRecord(
                    tool_name=tool_name,
                    price_usd=pricing.price_usd,
                    currency=self.billing.pricing.currency,
                    status=status,
                    latency_ms=latency_ms,
                    client_id=client_id,
                    paid_via=paid_via,
                    params=dict(context.message.arguments or {}),
                )
            )


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
        tool_name = _paid_tool_call(body, self.billing)
        if tool_name is None:
            return await self.app(scope, _replay(messages, receive), send)

        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        decision = self.billing.check_x402(tool_name, headers)
        if not decision.ok:
            return await _send_402(send, decision.challenge)
        return await self.app(scope, _replay(messages, receive), send)


async def _buffer_body(receive):
    body = b""
    messages = []
    more = True
    while more:
        message = await receive()
        messages.append(message)
        if message["type"] == "http.request":
            body += message.get("body", b"")
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


def _paid_tool_call(body: bytes, billing: BillingContext) -> str | None:
    try:
        payload = json.loads(body or b"{}")
    except json.JSONDecodeError:
        return None
    calls = payload if isinstance(payload, list) else [payload]
    for call in calls:
        if not isinstance(call, dict) or call.get("method") != "tools/call":
            continue
        name = (call.get("params") or {}).get("name")
        if name and billing.is_paid(name):
            return name
    return None


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
