"""J6: signed audit bundle — assemble, sign, verify, tamper-detect, serve."""

from __future__ import annotations

import json

import pytest

from core.auditbundle import AuditBundleStore, assemble, sign, verify
from core.models import (
    Leg, Opportunity, OpportunityKind, OrderbookLevel, OrderbookSnapshot, Side, Venue,
)
from datetime import datetime, timezone


def _opp():
    return Opportunity(kind=OpportunityKind.CROSS_VENUE, title="btc arb", category="crypto",
                       realizable_edge=0.04, max_size_usd=500.0,
                       legs=[Leg(venue=Venue.POLYMARKET, market_id="pm", side=Side.YES),
                             Leg(venue=Venue.KALSHI, market_id="kx", side=Side.NO)])


def _getters():
    from core.models import Market

    markets = {
        "pm": Market(venue=Venue.POLYMARKET, market_id="pm", title="a", yes_price=0.6,
                     no_price=0.4, volume_usd=1_000_000),
        "kx": Market(venue=Venue.KALSHI, market_id="kx", title="b", yes_price=0.55,
                     no_price=0.45, volume_usd=500_000),
    }

    def market_getter(venue, mid):
        return markets.get(mid)

    def book_getter(venue, mid):
        return OrderbookSnapshot(venue=Venue.KALSHI, market_id=mid,
                                 as_of=datetime.now(timezone.utc),
                                 yes_bids=[OrderbookLevel(price=0.5, size_usd=100.0)],
                                 yes_asks=[OrderbookLevel(price=0.6, size_usd=100.0)])

    return market_getter, book_getter


# --- assembly ---------------------------------------------------------------
def test_assemble_captures_leg_evidence():
    mg, bg = _getters()
    payload = assemble(_opp(), mg, bg)
    assert payload["realizable_edge"] == 0.04
    assert len(payload["legs"]) == 2
    leg = payload["legs"][0]
    assert leg["market_id"] == "pm" and leg["yes_price"] == 0.6
    assert leg["book_top"]["yes_bids"]


# --- sign + verify ----------------------------------------------------------
def test_sign_and_verify_ed25519(monkeypatch):
    from predmarket_mcp import provenance as prov

    monkeypatch.setenv("SIGNING_KEY_ED25519", "00" * 31 + "09")
    prov._ed25519_key.cache_clear()
    mg, bg = _getters()
    signed = sign(assemble(_opp(), mg, bg))
    assert signed["provenance"]["alg"] == "Ed25519"
    assert verify(signed) is True
    prov._ed25519_key.cache_clear()


def test_tampering_breaks_verification(monkeypatch):
    from predmarket_mcp import provenance as prov

    monkeypatch.setenv("SIGNING_KEY_ED25519", "00" * 31 + "09")
    prov._ed25519_key.cache_clear()
    mg, bg = _getters()
    signed = sign(assemble(_opp(), mg, bg))
    signed["bundle"]["realizable_edge"] = 0.99  # forge a bigger edge
    assert verify(signed) is False               # signature no longer matches
    prov._ed25519_key.cache_clear()


def test_unsigned_bundle_does_not_verify(monkeypatch):
    from predmarket_mcp import provenance as prov

    monkeypatch.delenv("SIGNING_KEY_ED25519", raising=False)
    monkeypatch.delenv("SIGNING_KEY", raising=False)
    prov._ed25519_key.cache_clear()
    mg, bg = _getters()
    signed = sign(assemble(_opp(), mg, bg))
    assert signed["provenance"]["signed"] is False
    assert verify(signed) is False
    prov._ed25519_key.cache_clear()


# --- store ------------------------------------------------------------------
def test_store_roundtrip(tmp_path):
    store = AuditBundleStore(db_url=f"sqlite:///{tmp_path / 'a.db'}")
    mg, bg = _getters()
    signed = sign(assemble(_opp(), mg, bg))
    bid = store.save(signed)
    assert store.get(bid)["bundle"]["title"] == "btc arb"
    assert store.get("nope") is None
    assert store.count() == 1


# --- integration: tool opt-in + public endpoint -----------------------------
@pytest.mark.asyncio
async def test_find_mispricing_audit_and_public_endpoint(client, monkeypatch):
    import httpx

    from predmarket_mcp.server import build_http_app

    r = (await client.call_tool("find_mispricing", {"min_edge": 0.0, "audit": True})).data
    ids = [o["audit_bundle_id"] for o in r["opportunities"]]
    assert ids and all(ids)

    app = build_http_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as ac:
        got = await ac.get(f"/audit/{ids[0]}")
        assert got.status_code == 200
        assert "bundle" in got.json() and "provenance" in got.json()
        assert (await ac.get("/audit/does-not-exist")).status_code == 404


@pytest.mark.asyncio
async def test_find_mispricing_no_audit_by_default(client):
    r = (await client.call_tool("find_mispricing", {"min_edge": 0.0})).data
    assert all("audit_bundle_id" not in o for o in r["opportunities"])
