"""Real x402 facilitator tests (mocked HTTP — no network, no chain)."""

from __future__ import annotations

import httpx
import pytest

from predmarket_mcp.billing.x402 import (
    HttpFacilitator,
    MockFacilitator,
    PaymentError,
    PaymentRequirement,
    build_facilitator,
)
from predmarket_mcp.config import Settings

REQ = PaymentRequirement(
    tool_name="find_mispricing", price_usd=0.05, currency="USDC",
    network="base", pay_to="0xOperator",
)
PAYMENT = {
    "scheme": "exact", "network": "base",
    "payload": {"signature": "0xsig", "authorization": {"from": "0xPayer", "to": "0xOperator", "value": "50000"}},
}


def _factory(routes):
    def handler(request: httpx.Request) -> httpx.Response:
        for path, payload in routes.items():
            if path in request.url.path:
                return httpx.Response(200, json=payload)
        return httpx.Response(404, json={})
    return lambda: httpx.Client(base_url="https://facilitator.example", transport=httpx.MockTransport(handler))


def test_verify_and_settle_returns_receipt_with_tx():
    fac = HttpFacilitator("https://facilitator.example", client_factory=_factory({
        "/verify": {"isValid": True},
        "/settle": {"success": True, "transaction": "0xdeadbeef", "payer": "0xPayer"},
    }))
    receipt = fac.verify_and_settle(PAYMENT, REQ)
    assert receipt.tx_hash == "0xdeadbeef"
    assert receipt.payer == "0xPayer"
    assert receipt.amount_usd == 0.05


def test_rejected_verification_raises():
    fac = HttpFacilitator("https://facilitator.example", client_factory=_factory({
        "/verify": {"isValid": False, "invalidReason": "bad_signature"},
    }))
    with pytest.raises(PaymentError, match="bad_signature"):
        fac.verify_and_settle(PAYMENT, REQ)


def test_failed_settlement_raises():
    fac = HttpFacilitator("https://facilitator.example", client_factory=_factory({
        "/verify": {"isValid": True},
        "/settle": {"success": False, "errorReason": "insufficient_funds"},
    }))
    with pytest.raises(PaymentError, match="insufficient_funds"):
        fac.verify_and_settle(PAYMENT, REQ)


def test_http_error_maps_to_payment_error():
    def factory():
        def handler(r):
            raise httpx.ConnectError("blocked")
        return httpx.Client(base_url="https://facilitator.example", transport=httpx.MockTransport(handler))
    fac = HttpFacilitator("https://facilitator.example", client_factory=factory)
    with pytest.raises(PaymentError):
        fac.verify_and_settle(PAYMENT, REQ)


def test_build_facilitator_selects_by_config():
    with_url = Settings(x402_facilitator_url="https://facilitator.example")
    without = Settings(x402_facilitator_url=None)
    assert isinstance(build_facilitator(with_url), HttpFacilitator)
    assert isinstance(build_facilitator(without), MockFacilitator)
