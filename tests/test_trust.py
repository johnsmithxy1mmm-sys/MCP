"""B6: Trust v2 — Merkle commitments, Ed25519 provenance, public endpoints."""

from __future__ import annotations

import base64

import pytest

from core import merkle


# --- Merkle -----------------------------------------------------------------
def test_merkle_root_stable_and_order_sensitive():
    a = [{"id": 1}, {"id": 2}, {"id": 3}]
    assert merkle.merkle_root(a) == merkle.merkle_root([{"id": 1}, {"id": 2}, {"id": 3}])
    assert merkle.merkle_root(a) != merkle.merkle_root([{"id": 2}, {"id": 1}, {"id": 3}])
    assert merkle.merkle_root([]) is None


def test_merkle_proof_verifies_and_detects_tampering():
    records = [{"id": i, "edge": i / 10} for i in range(5)]  # odd count exercises tail dup
    root = merkle.merkle_root(records)
    for i in range(len(records)):
        proof = merkle.merkle_proof(records, i)
        assert merkle.verify_proof(records[i], proof, root)
    # A tampered record fails against the original proof.
    bad = dict(records[2], edge=99.0)
    assert not merkle.verify_proof(bad, merkle.merkle_proof(records, 2), root)


# --- Ed25519 provenance -----------------------------------------------------
def _seed_hex():
    return "00" * 31 + "07"  # deterministic 32-byte seed


def test_provenance_uses_ed25519_when_keyed(monkeypatch):
    import importlib

    from predmarket_mcp import provenance as prov

    monkeypatch.setenv("SIGNING_KEY_ED25519", _seed_hex())
    prov._ed25519_key.cache_clear()

    payload = {"hit_rate": 0.6}
    block = prov.provenance(payload)
    assert block["alg"] == "Ed25519" and block["signed"] is True

    info = prov.public_key_info()
    assert info is not None and info["alg"] == "Ed25519"

    # Verify the signature against the served public key.
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    raw = base64.b64decode(info["public_key_b64"])
    pub = Ed25519PublicKey.from_public_bytes(raw)
    sig = base64.b64decode(block["signature"])
    pub.verify(sig, prov._canonical(payload).encode())  # raises if invalid
    prov._ed25519_key.cache_clear()


def test_provenance_falls_back_to_unsigned(monkeypatch):
    from predmarket_mcp import provenance as prov

    monkeypatch.delenv("SIGNING_KEY_ED25519", raising=False)
    monkeypatch.delenv("SIGNING_KEY", raising=False)
    prov._ed25519_key.cache_clear()
    block = prov.provenance({"x": 1})
    assert block["signed"] is False
    prov._ed25519_key.cache_clear()


# --- public HTTP endpoints --------------------------------------------------
@pytest.mark.asyncio
async def test_public_track_record_endpoint():
    import httpx

    from predmarket_mcp.server import build_http_app

    app = build_http_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.get("/track-record")
        assert r.status_code == 200
        body = r.json()
        assert "track_record" in body and "commitment" in body
        assert "merkle_root" in body["commitment"]

        pk = await ac.get("/pubkey")
        assert pk.status_code in (200, 404)  # 404 when no Ed25519 key configured
