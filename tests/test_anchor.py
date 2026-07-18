"""C6: on-chain anchoring of the track-record Merkle root."""

from __future__ import annotations

import pytest

from core import anchor
from core.anchor import AnchorStore, MockChainAnchor, build_anchor


def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv("ANCHOR_ENABLED", raising=False)
    assert anchor.anchor_track_record() is None
    assert anchor.latest_anchor() is None


def test_mock_chain_is_deterministic():
    a = MockChainAnchor("base-sepolia")
    r1 = a.submit("deadbeef")
    r2 = a.submit("deadbeef")
    assert r1 == r2
    assert r1["tx_hash"].startswith("0x") and r1["simulated"] is True


def test_build_anchor_mock_without_rpc(monkeypatch):
    monkeypatch.delenv("ANCHOR_RPC_URL", raising=False)
    assert isinstance(build_anchor(), MockChainAnchor)


def test_maybe_anchor_records_and_dedupes(tmp_path):
    store = AnchorStore(db_url=f"sqlite:///{tmp_path / 'a.db'}")
    chain = MockChainAnchor("base-sepolia")

    first = store.maybe_anchor("root-A", 3, chain)
    assert first["tx_hash"] and first["root"] == "root-A"
    assert store.count() == 1
    # Same root -> no new anchor.
    again = store.maybe_anchor("root-A", 3, chain)
    assert store.count() == 1 and again["root"] == "root-A"


def test_maybe_anchor_respects_min_interval(tmp_path, monkeypatch):
    monkeypatch.setenv("ANCHOR_MIN_INTERVAL", "9999")
    store = AnchorStore(db_url=f"sqlite:///{tmp_path / 'a.db'}")
    chain = MockChainAnchor("base-sepolia")
    store.maybe_anchor("root-A", 1, chain)
    # A new root arrives but the interval hasn't elapsed -> keep the old anchor.
    latest = store.maybe_anchor("root-B", 2, chain)
    assert latest["root"] == "root-A"
    assert store.count() == 1


def test_none_root_is_noop(tmp_path):
    store = AnchorStore(db_url=f"sqlite:///{tmp_path / 'a.db'}")
    assert store.maybe_anchor(None, 0, MockChainAnchor("base-sepolia")) is None


@pytest.mark.asyncio
async def test_public_track_record_includes_anchor_when_enabled(monkeypatch):
    import httpx

    from predmarket_mcp import deps
    from predmarket_mcp.server import build_http_app

    monkeypatch.setenv("ANCHOR_ENABLED", "on")
    monkeypatch.setenv("ANCHOR_MIN_INTERVAL", "0")
    # Seed one resolved record so the Merkle root is non-empty.
    from core.reconciliation import get_reconciliation
    store = get_reconciliation()
    with store._connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO flagged (opp_id, ts, kind, title, predicted_edge, "
            "implied_prob, legs, resolved, outcome) VALUES ('a','t','bundle','m',0.0,0.5,'[]',1,1)"
        )
    anchor.get_anchor_store.cache_clear()

    app = build_http_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.get("/track-record")
        assert r.status_code == 200
        body = r.json()
        assert "onchain_anchor" in body
        assert body["onchain_anchor"]["tx_hash"]
