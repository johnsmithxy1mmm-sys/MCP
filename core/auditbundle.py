"""Signed audit bundle for a flagged opportunity (J6, Trust v4).

The track record proves the *aggregate* is honest; an audit bundle proves a
*specific* edge existed at a specific moment. For a flagged opportunity it
assembles everything that went into the number — each leg's market snapshot,
top-of-book depth, the edge, a timestamp — and signs the canonical payload
(Ed25519, verifiable against ``/pubkey``). A buyer, or a skeptic, can fetch the
bundle from ``/audit/{id}`` and independently confirm the opportunity was real
and priced as claimed, without trusting our word. This is the deepest, least
copyable trust primitive: not "our stats say we're good" but "here is the
cryptographically-signed evidence for this exact call."

Pure assembly + a SQLite store; signing/verification live in ``provenance``.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from .models import Opportunity


def _sqlite_path(db_url: str) -> str:
    if db_url.startswith("sqlite:///"):
        return db_url[len("sqlite:///"):]
    if db_url.startswith("sqlite://"):
        return db_url[len("sqlite://"):]
    return str(Path.cwd() / "audit.db")


def _book_top(book, n: int = 3) -> dict | None:
    if book is None:
        return None
    return {
        "yes_bids": [{"price": l.price, "size_usd": l.size_usd} for l in book.yes_bids[:n]],
        "yes_asks": [{"price": l.price, "size_usd": l.size_usd} for l in book.yes_asks[:n]],
    }


def assemble(opp: Opportunity, market_getter, book_getter, as_of: str | None = None) -> dict:
    """The canonical evidence payload for an opportunity (no signature yet)."""
    legs = []
    for leg in opp.legs:
        m = market_getter(leg.venue.value, leg.market_id)
        book = book_getter(leg.venue.value, leg.market_id)
        legs.append({
            "venue": leg.venue.value, "market_id": leg.market_id, "side": leg.side.value,
            "yes_price": m.yes_price if m else None,
            "volume_usd": m.volume_usd if m else None,
            "book_top": _book_top(book),
        })
    return {
        "source": "predmarket-mcp",
        "kind": opp.kind.value,
        "title": opp.title,
        "realizable_edge": opp.realizable_edge,
        "max_size_usd": opp.max_size_usd,
        "legs": legs,
        "as_of": as_of or datetime.now(timezone.utc).isoformat(),
    }


def bundle_id(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:24]


def sign(payload: dict) -> dict:
    """Wrap a payload with its id and a signed provenance block."""
    from predmarket_mcp.provenance import provenance

    return {"bundle_id": bundle_id(payload), "bundle": payload, "provenance": provenance(payload)}


def verify(signed: dict) -> bool:
    """True if the signed bundle's provenance verifies against its payload."""
    from predmarket_mcp.provenance import verify_block

    return verify_block(signed.get("bundle", {}), signed.get("provenance", {}))


class AuditBundleStore:
    def __init__(self, db_url: str | None = None):
        self._url = db_url or os.getenv("AUDIT_DB_URL", "sqlite:///audit.db")
        self._path = _sqlite_path(self._url)
        self._lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS audit_bundles ("
                         "  bundle_id TEXT PRIMARY KEY, ts TEXT NOT NULL, doc TEXT NOT NULL)")

    def save(self, signed: dict) -> str:
        bid = signed["bundle_id"]
        with self._lock, self._connect() as conn:
            conn.execute("INSERT OR IGNORE INTO audit_bundles (bundle_id, ts, doc) VALUES (?,?,?)",
                         (bid, datetime.now(timezone.utc).isoformat(), json.dumps(signed)))
        return bid

    def get(self, bundle_id: str) -> dict | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT doc FROM audit_bundles WHERE bundle_id = ?",
                               (bundle_id,)).fetchone()
        return json.loads(row["doc"]) if row else None

    def count(self) -> int:
        with self._lock, self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM audit_bundles").fetchone()[0]


@lru_cache(maxsize=1)
def get_store() -> AuditBundleStore:
    return AuditBundleStore()
