"""Signed provenance for tool responses (Trust v2).

Attaches a tamper-evident block to intelligence payloads: source, timestamp, and
a signature over the canonical data. An agent (or downstream auditor) can verify
the numbers came from this server unmodified — part of the trust story, alongside
the public, Merkle-committed track record.

Two signing rails, preferred in order:

* **Ed25519** (``SIGNING_KEY_ED25519`` = 32-byte hex/base64 seed) — asymmetric, so
  anyone can verify against the PUBLIC key served at ``/pubkey`` without holding a
  shared secret. This is the real trust upgrade.
* **HMAC-SHA256** (``SIGNING_KEY``) — symmetric fallback for setups that only need
  integrity between parties that already share a secret.

With neither key set, responses are marked ``signed: false`` rather than failing —
signing is a trust upgrade, not a hard dependency.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
from datetime import datetime, timezone
from functools import lru_cache


def _canonical(payload) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


# --- Ed25519 (asymmetric) ---------------------------------------------------
def _decode_seed(raw: str) -> bytes | None:
    """Accept a 32-byte Ed25519 seed as hex or base64."""
    for decoder in (lambda s: binascii.unhexlify(s), lambda s: base64.b64decode(s)):
        try:
            seed = decoder(raw.strip())
            if len(seed) == 32:
                return seed
        except (binascii.Error, ValueError):
            continue
    return None


@lru_cache(maxsize=1)
def _ed25519_key():
    """Private Ed25519 key from SIGNING_KEY_ED25519, or None (missing key/lib)."""
    raw = os.getenv("SIGNING_KEY_ED25519")
    if not raw:
        return None
    seed = _decode_seed(raw)
    if seed is None:
        return None
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        return Ed25519PrivateKey.from_private_bytes(seed)
    except Exception:
        return None


def _sign_ed25519(payload) -> str | None:
    key = _ed25519_key()
    if key is None:
        return None
    return base64.b64encode(key.sign(_canonical(payload).encode())).decode()


def public_key_info() -> dict | None:
    """Public Ed25519 key (PEM + raw base64) for the ``/pubkey`` endpoint, or None."""
    key = _ed25519_key()
    if key is None:
        return None
    from cryptography.hazmat.primitives import serialization

    pub = key.public_key()
    pem = pub.public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()
    raw = pub.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return {"alg": "Ed25519", "public_key_pem": pem, "public_key_b64": base64.b64encode(raw).decode()}


# --- HMAC (symmetric fallback) ----------------------------------------------
def sign(payload, key: str | None = None) -> str | None:
    """HMAC-SHA256 signature (symmetric). Prefer :func:`provenance` which picks
    the strongest available rail."""
    key = key if key is not None else os.getenv("SIGNING_KEY")
    if not key:
        return None
    return hmac.new(key.encode(), _canonical(payload).encode(), hashlib.sha256).hexdigest()


def verify(payload, signature: str, key: str) -> bool:
    expected = hmac.new(key.encode(), _canonical(payload).encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")


def provenance(payload) -> dict:
    """Provenance block to embed alongside a payload, signed with the strongest
    available rail (Ed25519 > HMAC > unsigned)."""
    ed = _sign_ed25519(payload)
    if ed is not None:
        return {
            "source": "predmarket-mcp",
            "as_of": datetime.now(timezone.utc).isoformat(),
            "alg": "Ed25519",
            "signed": True,
            "signature": ed,
            "verify": "/pubkey",
        }
    signature = sign(payload)
    return {
        "source": "predmarket-mcp",
        "as_of": datetime.now(timezone.utc).isoformat(),
        "alg": "HMAC-SHA256",
        "signed": signature is not None,
        "signature": signature,
    }
