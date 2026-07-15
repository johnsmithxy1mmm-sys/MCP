"""Kalshi RSA-PSS request-signing tests (offline — local keypair)."""

from __future__ import annotations

import base64

import pytest

crypto = pytest.importorskip("cryptography")
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import padding, rsa  # noqa: E402

from core.adapters import kalshi  # noqa: E402


def _make_keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    return key.public_key(), pem


def test_signed_headers_verify_against_public_key(monkeypatch):
    public_key, pem = _make_keypair()
    monkeypatch.setenv("KALSHI_API_KEY_ID", "kid-123")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY", pem)

    headers = kalshi._auth_headers("GET", "/markets/KX-1/orderbook")
    assert headers["KALSHI-ACCESS-KEY"] == "kid-123"
    ts = headers["KALSHI-ACCESS-TIMESTAMP"]
    sig = base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"])

    # Reconstruct the signed message: ts + METHOD + base_path + rel_path.
    message = (ts + "GET" + kalshi._BASE_PATH + "/markets/KX-1/orderbook").encode()
    public_key.verify(  # raises on mismatch
        sig, message,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )


def test_tampered_message_fails_verification(monkeypatch):
    public_key, pem = _make_keypair()
    monkeypatch.setenv("KALSHI_API_KEY_ID", "kid-123")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY", pem)
    headers = kalshi._auth_headers("GET", "/markets")
    sig = base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"])
    wrong = (headers["KALSHI-ACCESS-TIMESTAMP"] + "GET" + kalshi._BASE_PATH + "/OTHER").encode()
    with pytest.raises(Exception):
        public_key.verify(
            sig, wrong,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )


def test_legacy_bearer_fallback(monkeypatch):
    monkeypatch.delenv("KALSHI_API_KEY_ID", raising=False)
    monkeypatch.delenv("KALSHI_PRIVATE_KEY", raising=False)
    monkeypatch.setenv("KALSHI_API_KEY", "legacy-token")
    assert kalshi._auth_headers("GET", "/markets") == {"Authorization": "Bearer legacy-token"}


def test_no_auth_when_unconfigured(monkeypatch):
    for var in ("KALSHI_API_KEY_ID", "KALSHI_PRIVATE_KEY", "KALSHI_PRIVATE_KEY_PATH", "KALSHI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    assert kalshi._auth_headers("GET", "/markets") == {}
