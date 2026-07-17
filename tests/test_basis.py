"""C2: resolution-criteria diff / settlement basis risk."""

from __future__ import annotations

import pytest

from core import basis
from core.basis import heuristic_basis_risk
from core.models import Market, Venue


def _m(title, venue=Venue.POLYMARKET, category="crypto"):
    return Market(venue=venue, market_id=f"{venue.value}-x", title=title,
                  yes_price=0.5, no_price=0.5, category=category)


def test_same_framing_is_low_basis_risk():
    a = _m("Bitcoin close above $100k at end of 2026", Venue.POLYMARKET)
    b = _m("Bitcoin closing above $100k at end of 2026", Venue.KALSHI)
    out = heuristic_basis_risk(a, b)
    assert out["basis_risk"] < 0.25
    assert out["same_contract"] is True


def test_framing_mismatch_raises_basis_risk():
    # Snapshot ("close ... at end") vs cumulative ("reach ... by") is a real
    # settlement difference even though the titles look similar.
    a = _m("Bitcoin close above $100k at end of 2026", Venue.POLYMARKET)
    b = _m("Will Bitcoin reach $100k by end of 2026?", Venue.KALSHI)
    out = heuristic_basis_risk(a, b)
    assert out["basis_risk"] >= 0.35
    assert out["same_contract"] is False
    assert any("framing" in d for d in out["differences"])


def test_different_sources_raise_risk():
    a = _m("BTC above 100k per Coinbase at end of 2026", Venue.POLYMARKET)
    b = _m("BTC above 100k per Binance at end of 2026", Venue.KALSHI)
    out = heuristic_basis_risk(a, b)
    assert any("source" in d for d in out["differences"])


def test_assess_basis_uses_heuristic_when_llm_disabled(monkeypatch):
    monkeypatch.delenv("ANALYST_ENABLED", raising=False)
    out = basis.assess_basis(_m("x close at end 2026"), _m("x close at end 2026", Venue.KALSHI))
    assert out["source"] == "heuristic"


def test_assess_basis_llm_path(monkeypatch):
    import httpx

    from core.analyst import LLMAnalyst

    monkeypatch.setenv("ANALYST_ENABLED", "on")

    def factory():
        def handler(req):
            return httpx.Response(200, json={"content": [
                {"type": "tool_use", "name": "report_basis_risk",
                 "input": {"same_contract": False, "basis_risk": 0.8,
                           "differences": ["source differs"], "confidence": 0.9}},
            ]})
        return httpx.Client(base_url="https://x", transport=httpx.MockTransport(handler))

    monkeypatch.setattr("core.basis.get_analyst",
                        lambda: LLMAnalyst(api_key="k", client_factory=factory))
    out = basis.assess_basis(_m("a"), _m("b", Venue.KALSHI))
    assert out["source"] == "llm"
    assert out["basis_risk"] == 0.8 and out["same_contract"] is False


@pytest.mark.asyncio
async def test_compare_reports_basis_risk(client):
    r = (await client.call_tool(
        "compare_across_venues", {"event": "bitcoin above 100k end of 2026"}
    )).data
    assert r["matched"] is True
    assert 0 <= r["basis_risk"] <= 1
    assert isinstance(r["same_contract"], bool)
    assert "resolution_differences" in r
