"""Merkle tree over records for tamper-evident, auditable track records (B6).

A track record is only trustworthy if you can't quietly edit history. Hashing the
sequence of flagged/resolved records into a Merkle root lets the server publish
one small root that commits to every entry: anyone can later be handed a single
record plus an O(log n) proof and verify it was included, without trusting the
server to re-serve the whole set honestly.

Pure/stdlib (hashlib) — no dependencies, fully offline.
"""

from __future__ import annotations

import hashlib
import json


def _leaf_hash(record) -> str:
    data = json.dumps(record, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(b"\x00" + data.encode()).hexdigest()  # 0x00 = leaf domain


def _node_hash(left: str, right: str) -> str:
    return hashlib.sha256(b"\x01" + bytes.fromhex(left) + bytes.fromhex(right)).hexdigest()


def merkle_root(records: list) -> str | None:
    """Merkle root over ``records`` (order-sensitive), or None if empty."""
    if not records:
        return None
    level = [_leaf_hash(r) for r in records]
    while len(level) > 1:
        nxt = []
        for i in range(0, len(level), 2):
            left = level[i]
            right = level[i + 1] if i + 1 < len(level) else left  # duplicate odd tail
            nxt.append(_node_hash(left, right))
        level = nxt
    return level[0]


def merkle_proof(records: list, index: int) -> list[dict]:
    """Inclusion proof for ``records[index]``: sibling hashes bottom-up."""
    if not (0 <= index < len(records)):
        raise IndexError("index out of range")
    level = [_leaf_hash(r) for r in records]
    proof: list[dict] = []
    idx = index
    while len(level) > 1:
        sibling = idx ^ 1
        if sibling < len(level):
            proof.append({"hash": level[sibling], "side": "right" if sibling > idx else "left"})
        else:
            proof.append({"hash": level[idx], "side": "right"})  # duplicated odd tail
        nxt = []
        for i in range(0, len(level), 2):
            left = level[i]
            right = level[i + 1] if i + 1 < len(level) else left
            nxt.append(_node_hash(left, right))
        level = nxt
        idx //= 2
    return proof


def verify_proof(record, proof: list[dict], root: str) -> bool:
    """Recompute the root from a leaf + its proof and compare."""
    h = _leaf_hash(record)
    for step in proof:
        if step["side"] == "left":
            h = _node_hash(step["hash"], h)
        else:
            h = _node_hash(h, step["hash"])
    return h == root
