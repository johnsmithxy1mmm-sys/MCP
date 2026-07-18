"""On-chain anchoring of the track-record Merkle root (C6, Trust v3).

merkle.py commits every resolved record to one root; provenance signs it. But an
auditor still has to trust that WE served the honest root. Anchoring that root in
a public blockchain removes us from the trust equation: the root is timestamped
by a chain we don't control, so "we didn't rewrite history" is provable against
the ledger, not against our own endpoint. This is what makes the track record
*provable* alpha rather than merely *signed* alpha.

Pluggable submitter (mirrors the x402 facilitator pattern):
* ``MockChainAnchor`` (default) — deterministic pseudo-transaction from the root,
  so the mechanism is exercised and tested offline with zero infra.
* ``EvmChainAnchor`` — real submission to an EVM chain (Base) when ``ANCHOR_RPC_URL``
  is set; degrades to None on any failure. # TODO: sign + eth_sendRawTransaction.

All anchoring is OFF unless ``ANCHOR_ENABLED``; anchors persist in SQLite.

  ANCHOR_ENABLED      unset (default) | on
  ANCHOR_NETWORK      base-sepolia (default)
  ANCHOR_RPC_URL      set to submit real transactions (else MockChainAnchor)
  ANCHOR_MIN_INTERVAL seconds between anchors (default 3600)
  ANCHOR_DB_URL       sqlite:///anchors.db
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import threading
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path


def anchor_enabled() -> bool:
    return os.getenv("ANCHOR_ENABLED", "").lower() in ("1", "on", "true", "yes")


def _sqlite_path(db_url: str) -> str:
    if db_url.startswith("sqlite:///"):
        return db_url[len("sqlite:///"):]
    if db_url.startswith("sqlite://"):
        return db_url[len("sqlite://"):]
    return str(Path.cwd() / "anchors.db")


class ChainAnchor(ABC):
    @abstractmethod
    def submit(self, root: str) -> dict | None:
        """Submit a root; return {tx_hash, network, block} or None on failure."""


class MockChainAnchor(ChainAnchor):
    """Deterministic, offline stand-in — a pseudo-tx derived from the root."""

    def __init__(self, network: str):
        self.network = network

    def submit(self, root: str) -> dict | None:
        tx = "0x" + hashlib.sha256(f"anchor|{root}".encode()).hexdigest()
        block = int(tx[2:10], 16) % 10_000_000
        return {"tx_hash": tx, "network": self.network, "block": block, "simulated": True}


class EvmChainAnchor(ChainAnchor):
    """Real EVM anchor via JSON-RPC (structural; needs a funded signer).

    Writes the root as transaction calldata to a burn/self address. Requires
    ANCHOR_RPC_URL + a signer; any failure returns None so trust degrades to the
    signed (but un-anchored) root rather than crashing. # TODO: real signing.
    """

    def __init__(self, rpc_url: str, network: str):
        self.rpc_url = rpc_url
        self.network = network

    def submit(self, root: str) -> dict | None:  # pragma: no cover - needs a chain
        return None  # TODO: sign + eth_sendRawTransaction; degrade until then


def build_anchor() -> ChainAnchor:
    network = os.getenv("ANCHOR_NETWORK", "base-sepolia")
    rpc = os.getenv("ANCHOR_RPC_URL")
    return EvmChainAnchor(rpc, network) if rpc else MockChainAnchor(network)


class AnchorStore:
    def __init__(self, db_url: str | None = None):
        self._url = db_url or os.getenv("ANCHOR_DB_URL", "sqlite:///anchors.db")
        self._path = _sqlite_path(self._url)
        self._lock = threading.Lock()
        self._min_interval = float(os.getenv("ANCHOR_MIN_INTERVAL", "3600"))
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS anchors ("
                "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
                "  root TEXT NOT NULL, ts TEXT NOT NULL,"
                "  tx_hash TEXT, network TEXT, block INTEGER, leaf_count INTEGER)"
            )

    def latest(self) -> dict | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM anchors ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def count(self) -> int:
        with self._lock, self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM anchors").fetchone()[0]

    def _save(self, root: str, receipt: dict, leaf_count: int) -> dict:
        rec = {
            "root": root, "ts": datetime.now(timezone.utc).isoformat(),
            "tx_hash": receipt.get("tx_hash"), "network": receipt.get("network"),
            "block": receipt.get("block"), "leaf_count": leaf_count,
        }
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO anchors (root, ts, tx_hash, network, block, leaf_count) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (rec["root"], rec["ts"], rec["tx_hash"], rec["network"],
                 rec["block"], rec["leaf_count"]),
            )
        return rec

    def maybe_anchor(self, root: str | None, leaf_count: int, anchor: ChainAnchor) -> dict | None:
        """Anchor ``root`` if it's new and the min interval has elapsed."""
        if not root:
            return None
        latest = self.latest()
        if latest is not None:
            if latest["root"] == root:
                return latest  # unchanged — already anchored
            age = time.time() - datetime.fromisoformat(latest["ts"]).timestamp()
            if age < self._min_interval:
                return latest  # rate-limit chain writes
        receipt = anchor.submit(root)
        if receipt is None:
            return latest
        return self._save(root, receipt, leaf_count)


@lru_cache(maxsize=1)
def get_anchor_store() -> AnchorStore:
    return AnchorStore()


def anchor_track_record() -> dict | None:
    """Anchor the current reconciliation Merkle root if enabled + due. Returns the
    latest anchor (or None when disabled/no data)."""
    if not anchor_enabled():
        return None
    from .reconciliation import get_reconciliation

    commitment = get_reconciliation().merkle_commitment()
    return get_anchor_store().maybe_anchor(
        commitment["merkle_root"], commitment["leaf_count"], build_anchor()
    )


def latest_anchor() -> dict | None:
    """Latest stored anchor for the public record (read-only; no submission)."""
    if not anchor_enabled():
        return None
    return get_anchor_store().latest()
