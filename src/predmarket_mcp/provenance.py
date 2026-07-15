"""Signed provenance for tool responses.

Attaches a tamper-evident block to intelligence payloads: source, timestamp, and
an HMAC-SHA256 signature over the canonical data. An agent (or a downstream
auditor) can verify the numbers came from this server unmodified — part of the
trust story, alongside the public track record.

The signing key is an operator secret from the environment (SIGNING_KEY). With
no key set, responses are marked ``signed: false`` rather than failing — signing
is a trust upgrade, not a hard dependency.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from datetime import datetime, timezone


def _canonical(payload) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def sign(payload, key: str | None = None) -> str | None:
    key = key if key is not None else os.getenv("SIGNING_KEY")
    if not key:
        return None
    return hmac.new(key.encode(), _canonical(payload).encode(), hashlib.sha256).hexdigest()


def verify(payload, signature: str, key: str) -> bool:
    expected = hmac.new(key.encode(), _canonical(payload).encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")


def provenance(payload) -> dict:
    """Provenance block to embed alongside a signed payload."""
    signature = sign(payload)
    return {
        "source": "predmarket-mcp",
        "as_of": datetime.now(timezone.utc).isoformat(),
        "alg": "HMAC-SHA256",
        "signed": signature is not None,
        "signature": signature,
    }
