"""C5: value-based dynamic pricing (get_market_history scales with range).

Money-safety: the charge is bounded, monotone in range, never below base, and a
batch pays exactly the sum — never lost, never doubled.
"""

from __future__ import annotations

import json

import pytest

from predmarket_mcp.billing.tiers import load_pricing
from predmarket_mcp.config import Settings
from predmarket_mcp.billing.middleware import BillingContext, X402Middleware
from predmarket_mcp.billing.x402 import encode_payment_header, _to_atomic

from tests.test_billing import _run_asgi, _ok_downstream


def _hist_args(days):
    return {"venue": "polymarket", "market_id": "m",
            "from_ts": "2026-01-01T00:00:00Z",
            "to_ts": f"2026-01-{1 + days:02d}T00:00:00Z"}


# --- price_for -------------------------------------------------------------
def test_history_price_scales_with_range_and_caps():
    p = load_pricing()
    base = p.get("get_market_history").price_usd
    short = p.price_for("get_market_history", _hist_args(2))
    long = p.price_for("get_market_history", _hist_args(29))
    assert short == pytest.approx(base, abs=1e-3)     # ~1-2 days ~ base
    assert long > short                                # wider range costs more
    # Cap holds for an absurd range.
    huge = p.price_for("get_market_history",
                       {"from_ts": "2000-01-01T00:00:00Z", "to_ts": "2100-01-01T00:00:00Z"})
    assert huge <= 0.10


def test_flat_tools_ignore_args():
    p = load_pricing()
    assert p.price_for("find_mispricing", {"min_edge": 0.02}) == 0.05
    assert p.price_for("compare_across_venues", {"event": "x"}) == 0.02


def test_bad_range_falls_back_to_base():
    p = load_pricing()
    base = p.get("get_market_history").price_usd
    assert p.price_for("get_market_history",
                       {"from_ts": "nope", "to_ts": "also-nope"}) == base


# --- enforcement: exact amount, no under/overpay ----------------------------
def _paid_billing(tmp_path) -> BillingContext:
    return BillingContext(Settings(
        paid_enabled=True, payment_rail="x402",
        metering_db_url=f"sqlite:///{tmp_path / 'p.db'}",
    ))


def _header(billing, value):
    return encode_payment_header({
        "scheme": "exact", "network": billing.settings.x402_network,
        "payload": {"signature": "0xsig",
                    "authorization": {"from": "0xp", "to": "0xop", "value": str(value)}},
    }).encode()


def _hist_body(days):
    return json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": "get_market_history", "arguments": _hist_args(days)}}).encode()


@pytest.mark.asyncio
async def test_wide_history_query_must_pay_the_scaled_amount(tmp_path):
    billing = _paid_billing(tmp_path)
    mw = X402Middleware(_ok_downstream, billing)
    price = billing.pricing.price_for("get_market_history", _hist_args(29))
    assert price > billing.pricing.get("get_market_history").price_usd  # actually scaled

    # Paying only the base price for a wide query is rejected as underpayment.
    base_atomic = _to_atomic(billing.pricing.get("get_market_history").price_usd)
    under, ubody = await _run_asgi(mw, _hist_body(29), [(b"x-payment", _header(billing, base_atomic))])
    assert under == 402 and "insufficient" in json.loads(ubody).get("error_detail", "")

    # Paying the scaled amount passes — exactly, not doubled.
    ok, _ = await _run_asgi(mw, _hist_body(29), [(b"x-payment", _header(billing, _to_atomic(price)))])
    assert ok == 200
    assert billing.receipts.count() == 1
