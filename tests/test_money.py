"""F9: revenue reconciliation, HMAC webhooks, latency metrics, market_id index."""

from __future__ import annotations

import hashlib
import hmac
import json

import httpx
import pytest

from predmarket_mcp.config import Settings
from predmarket_mcp.billing.middleware import BillingContext


# --- revenue reconciliation -------------------------------------------------
def test_revenue_summary_reconciles_charged_vs_settled(tmp_path):
    billing = BillingContext(Settings(
        paid_enabled=True, payment_rail="x402",
        metering_db_url=f"sqlite:///{tmp_path / 'm.db'}"))
    # Two paid calls charged, one receipt settled.
    from predmarket_mcp.billing.metering import UsageRecord
    from predmarket_mcp.billing.x402 import Receipt

    billing.metering.record(UsageRecord("find_mispricing", 0.05, "USDC", "ok", 1.0,
                                        "c1", "x402", {}))
    billing.metering.record(UsageRecord("search_markets", 0.0, "USDC", "ok", 1.0,
                                        "c1", None, {}))  # free -> not counted
    billing.receipts.save(Receipt("r1", "find_mispricing", 0.05, "USDC", "base", "p", "o", "0xtx"))

    s = billing.revenue_summary()
    assert s["charged_usd"] == 0.05 and s["charged_calls"] == 1  # free call excluded
    assert s["settled_usd"] == 0.05 and s["settled_receipts"] == 1
    assert s["discrepancy_usd"] == 0.0


@pytest.mark.asyncio
async def test_revenue_endpoint_requires_admin_token(monkeypatch):
    from predmarket_mcp.server import build_http_app

    monkeypatch.setenv("ADMIN_TOKEN", "s3cret")
    app = build_http_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as ac:
        assert (await ac.get("/revenue")).status_code == 403                     # no token
        assert (await ac.get("/revenue", headers={"x-admin-token": "wrong"})).status_code == 403
        ok = await ac.get("/revenue", headers={"x-admin-token": "s3cret"})
        assert ok.status_code == 200 and "charged_usd" in ok.json()


@pytest.mark.asyncio
async def test_revenue_disabled_without_admin_token(monkeypatch):
    from predmarket_mcp.server import build_http_app

    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    app = build_http_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as ac:
        assert (await ac.get("/revenue")).status_code == 404  # off unless configured


# --- HMAC-signed webhooks ---------------------------------------------------
def test_webhook_is_hmac_signed(monkeypatch):
    captured = {}

    class _Resp:
        status_code = 200

    class _Client:
        def __init__(self, *a, **k): ...
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, url, content, headers):
            captured["content"] = content
            captured["sig"] = headers.get("X-Signature")
            return _Resp()

    monkeypatch.setenv("ALERT_WEBHOOK_URL", "https://hook.example")
    monkeypatch.setenv("ALERT_WEBHOOK_SECRET", "topsecret")
    monkeypatch.setattr(httpx, "Client", _Client)

    from core.notifier import notify_alerts
    assert notify_alerts([{"a": 1}]) is True
    # The signature verifies against the exact body with the shared secret.
    expected = "sha256=" + hmac.new(b"topsecret", captured["content"], hashlib.sha256).hexdigest()
    assert captured["sig"] == expected


# --- latency metrics --------------------------------------------------------
def test_metrics_summary_renders_latency():
    from predmarket_mcp.ops import Metrics

    m = Metrics()
    m.observe("tool_latency_ms", 10)
    m.observe("tool_latency_ms", 30)
    text = m.render_prometheus()
    assert "predmarket_tool_latency_ms_sum 40" in text
    assert "predmarket_tool_latency_ms_count 2" in text


# --- market_id index --------------------------------------------------------
def test_live_repo_indexes_market_lookups(monkeypatch):
    from core.adapters import base
    from core.models import Venue

    KX = {"markets": [{"ticker": "KX-1", "title": "t", "yes_bid": 60, "yes_ask": 62,
                       "volume": 1000, "status": "open"}]}

    def make_client(base_url, timeout=None):
        def handler(req):
            if "kalshi" in str(req.url) and "markets" in str(req.url):
                return httpx.Response(200, json=KX)
            return httpx.Response(200, json={"markets": []})
        return httpx.Client(base_url=base_url, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(base, "make_client", make_client)
    base._CLIENTS.clear()
    from core import live
    repo = live.LiveRepo()
    m = repo.get_market("kalshi", "KX-1")
    assert m is not None and m.market_id == "KX-1"
    # Second lookup is served from the O(1) index, not a rescan.
    assert repo._by_id[("kalshi", "KX-1")] is m
    assert repo.get_market("kalshi", "nope") is None
