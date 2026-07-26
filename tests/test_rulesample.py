"""K3: client-sampled rulebook basis analysis via MCP sampling."""

from __future__ import annotations

import pytest

from core import rulesample
from core.models import Market, Venue


def _m(mid, title, venue=Venue.KALSHI):
    return Market(venue=venue, market_id=mid, title=title, category="crypto",
                  yes_price=0.6, no_price=0.4, volume_usd=100_000)


# --- parsing ----------------------------------------------------------------
def test_parse_valid_json():
    out = rulesample.parse(
        '{"same_contract": false, "basis_risk": 0.7, '
        '"differences": ["source", "snapshot"], "confidence": 0.9}')
    assert out["basis_risk"] == 0.7 and out["same_contract"] is False
    assert out["source"] == "client_sample"
    assert out["differences"] == ["source", "snapshot"]


def test_parse_tolerates_code_fence_and_prose():
    text = "Here is my verdict:\n```json\n{\"basis_risk\": 0.3}\n```\nDone."
    out = rulesample.parse(text)
    assert out is not None and out["basis_risk"] == 0.3
    assert out["same_contract"] is False  # no explicit key -> default risk<0.25


def test_parse_clamps_and_defaults():
    out = rulesample.parse('{"basis_risk": 5, "confidence": "oops"}')
    assert out["basis_risk"] == 1.0        # clamped to [0,1]
    assert out["confidence"] == 0.6        # bad confidence -> default


def test_parse_rejects_garbage():
    assert rulesample.parse("not json at all") is None
    assert rulesample.parse("") is None
    assert rulesample.parse('{"no_risk_key": 1}') is None


def test_sampling_disabled_by_default(monkeypatch):
    monkeypatch.delenv("RULEBOOK_SAMPLING", raising=False)
    assert rulesample.sampling_enabled() is False


def test_build_user_message_names_both_markets():
    msg = rulesample.build_user_message(
        _m("a", "BTC above 100k (Coinbase close)"),
        _m("b", "BTC above 100k (Binance)", venue=Venue.POLYMARKET))
    assert "Coinbase" in msg and "Binance" in msg and "kalshi" in msg


# --- end-to-end through compare_across_venues -------------------------------
@pytest.mark.asyncio
async def test_compare_uses_client_sample_when_enabled(monkeypatch):
    monkeypatch.setenv("RULEBOOK_SAMPLING", "on")
    from fastmcp import Client
    from predmarket_mcp.server import mcp

    async def sampling_handler(messages, params, context):
        # The client's "model" returns a strict JSON basis verdict.
        return ('{"same_contract": false, "basis_risk": 0.66, '
                '"differences": ["different data source"], "confidence": 0.88}')

    async with Client(mcp, sampling_handler=sampling_handler) as c:
        r = (await c.call_tool(
            "compare_across_venues",
            {"event": "bitcoin above 100k end of 2026"})).data
        assert r["basis_risk"] == 0.66            # client verdict won
        assert r["same_contract"] is False
        assert "different data source" in r["resolution_differences"]


@pytest.mark.asyncio
async def test_compare_degrades_without_sampling_support(monkeypatch, client):
    # Flag on, but the default test client advertises no sampling handler -> the
    # tool must fall back to the heuristic basis assessment, never error.
    monkeypatch.setenv("RULEBOOK_SAMPLING", "on")
    r = (await client.call_tool(
        "compare_across_venues", {"event": "bitcoin above 100k end of 2026"})).data
    assert "basis_risk" in r and 0.0 <= r["basis_risk"] <= 1.0
