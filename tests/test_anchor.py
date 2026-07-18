"""C6: on-chain anchoring of the track-record Merkle root."""

from __future__ import annotations

import pytest

from core import anchor
from core.anchor import AnchorStore, EvmChainAnchor, MockChainAnchor, build_anchor


class _FakeSigner:
    address = "0xabc0000000000000000000000000000000000001"

    def __init__(self):
        self.signed = None

    def sign(self, tx):
        self.signed = tx
        return "0xdeadbeefraw"


def _fake_rpc(sent):
    responses = {
        "eth_getTransactionCount": "0x5",
        "eth_chainId": "0x2105",  # Base
        "eth_getBlockByNumber": {"baseFeePerGas": "0x3b9aca00"},  # 1 gwei
        "eth_maxPriorityFeePerGas": "0x3b9aca00",
        "eth_sendRawTransaction": "0xTXHASH",
    }

    def rpc(method, params):
        sent.append((method, params))
        return responses[method]

    return rpc


def test_evm_anchor_signs_and_submits():
    sent = []
    signer = _FakeSigner()
    a = EvmChainAnchor("https://rpc", "base-sepolia", signer=signer, rpc=_fake_rpc(sent))
    out = a.submit("aa" * 32)  # 32-byte root
    assert out["tx_hash"] == "0xTXHASH"
    assert out["simulated"] is False
    # The root rode in the tx calldata; it was a 0-value self-send, EIP-1559.
    assert signer.signed["data"] == "0x" + "aa" * 32
    assert signer.signed["to"] == signer.address
    assert signer.signed["value"] == 0 and signer.signed["type"] == 2
    assert signer.signed["nonce"] == 5
    assert ("eth_sendRawTransaction", ["0xdeadbeefraw"]) in sent


def test_chain_id_env_accepts_decimal_and_hex(monkeypatch):
    # ANCHOR_CHAIN_ID is human-entered: "8453" (decimal) and "0x2105" (hex) are
    # the SAME chain (Base) and must produce the same signed chainId.
    for raw in ("8453", "0x2105"):
        monkeypatch.setenv("ANCHOR_CHAIN_ID", raw)
        signer = _FakeSigner()
        a = EvmChainAnchor("https://rpc", "base", signer=signer, rpc=_fake_rpc([]))
        assert a.submit("aa" * 32) is not None
        assert signer.signed["chainId"] == 8453, f"raw={raw!r}"
    monkeypatch.delenv("ANCHOR_CHAIN_ID", raising=False)


def test_evm_anchor_degrades_without_signer():
    a = EvmChainAnchor("https://rpc", "base-sepolia", private_key=None)
    assert a.submit("aa" * 32) is None  # no key/lib -> degrade, never crash


def test_evm_anchor_degrades_on_rpc_error():
    def boom(method, params):
        raise RuntimeError("rpc down")

    a = EvmChainAnchor("https://rpc", "base", signer=_FakeSigner(), rpc=boom)
    assert a.submit("aa" * 32) is None


def test_build_anchor_evm_when_rpc_set(monkeypatch):
    monkeypatch.setenv("ANCHOR_RPC_URL", "https://rpc")
    assert isinstance(build_anchor(), EvmChainAnchor)


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
