"""K7: reality feeds — market vs a real-world nowcast."""

from __future__ import annotations

import json

import httpx
import pytest

from core.models import Market, Venue
from core.reality import HttpNowcastProvider, divergence, get_provider


def _m(title="Bitcoin above 100k", yes=0.60):
    return Market(venue=Venue.KALSHI, market_id="x", title=title,
                  yes_price=yes, no_price=round(1 - yes, 4), volume_usd=100_000)


# --- pure divergence --------------------------------------------------------
def test_divergence_directions():
    assert divergence(0.60, 0.45)["direction"] == "market_rich"
    assert divergence(0.40, 0.55)["direction"] == "market_cheap"
    assert divergence(0.50, 0.50)["direction"] == "aligned"


def test_divergence_signal_threshold(monkeypatch):
    monkeypatch.setenv("REALITY_SIGNAL_MIN", "0.10")
    assert divergence(0.60, 0.45)["signal"] is True     # 0.15 >= 0.10
    assert divergence(0.60, 0.55)["signal"] is False    # 0.05 < 0.10


# --- provider ---------------------------------------------------------------
def _provider(handler):
    def factory():
        return httpx.Client(transport=httpx.MockTransport(handler))
    return HttpNowcastProvider("http://feed/nowcast", client_factory=factory)


def test_provider_reads_probability():
    p = _provider(lambda req: httpx.Response(200, json={"probability": 0.45}))
    assert p.implied_probability(_m()) == 0.45


def test_provider_degrades_on_bad_response():
    assert _provider(lambda req: httpx.Response(500)).implied_probability(_m()) is None
    assert _provider(lambda req: httpx.Response(200, json={})).implied_probability(_m()) is None
    assert _provider(
        lambda req: httpx.Response(200, text="not json")).implied_probability(_m()) is None


def test_get_provider_none_unless_enabled(monkeypatch):
    monkeypatch.delenv("REALITY_ENABLED", raising=False)
    assert get_provider() is None
    monkeypatch.setenv("REALITY_ENABLED", "on")
    monkeypatch.delenv("REALITY_NOWCAST_URL", raising=False)
    assert get_provider() is None                       # enabled but no feed url


# --- fusion into the house probability (K1 x K7) ----------------------------
def test_reality_feeds_house_probability():
    from core.houseview import fuse

    v = fuse(0.60, reality={"reality_probability": 0.40})
    sources = {c["source"] for c in v["components"]}
    assert "reality_nowcast" in sources
    assert v["house_probability"] < 0.60               # pulled toward reality


# --- through deps + evaluate_market -----------------------------------------
class _StubProvider:
    def implied_probability(self, market):
        return 0.42


def test_reality_divergence_via_deps(monkeypatch):
    import core.reality as reality
    from predmarket_mcp import deps

    monkeypatch.setattr(reality, "get_provider", lambda: _StubProvider())
    market = deps.get_market("kalshi", "kx-btc-100k-eoy26")
    out = deps.reality_divergence(market)
    assert out is not None and out["reality_probability"] == 0.42
    assert "direction" in out


@pytest.mark.asyncio
async def test_evaluate_market_carries_reality_when_enabled(monkeypatch, client):
    import core.reality as reality

    monkeypatch.setattr(reality, "get_provider", lambda: _StubProvider())
    r = (await client.call_tool(
        "evaluate_market", {"venue": "kalshi", "market_id": "kx-btc-100k-eoy26"})).data
    assert "reality" in r
    assert r["reality"]["reality_probability"] == 0.42


@pytest.mark.asyncio
async def test_evaluate_market_no_reality_when_disabled(client):
    r = (await client.call_tool(
        "evaluate_market", {"venue": "kalshi", "market_id": "kx-btc-100k-eoy26"})).data
    assert "reality" not in r                            # off by default
