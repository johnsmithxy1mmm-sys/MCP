"""Reconciliation + track record — the trust moat.

Records every flagged opportunity, then (once the underlying markets resolve)
computes what it *actually* realized. Aggregates into a public track record:
hit-rate, predicted-vs-realized edge slippage, and a Brier score on the implied
probabilities we surfaced. This turns "honest realizable edge" from a claim into
verifiable evidence — the reason an agent trusts these rails over building its own.

SQLite by default (zero infra); Postgres/Timescale DSN via RECON_DB_URL.
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

from .models import Opportunity, Side


def _sqlite_path(db_url: str) -> str:
    if db_url.startswith("sqlite:///"):
        return db_url[len("sqlite:///"):]
    if db_url.startswith("sqlite://"):
        return db_url[len("sqlite://"):]
    return str(Path.cwd() / "reconciliation.db")


def _opp_id(opp: Opportunity, legs: list[dict]) -> str:
    key = opp.kind.value + "|" + opp.title + "|" + json.dumps(
        [(l["market_id"], l["side"]) for l in legs], sort_keys=True
    )
    return hashlib.sha256(key.encode()).hexdigest()[:24]


class ReconciliationStore:
    def __init__(self, db_url: str | None = None):
        self._url = db_url or os.getenv("RECON_DB_URL", "sqlite:///reconciliation.db")
        self._path = _sqlite_path(self._url)
        self._lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS flagged (
                    opp_id TEXT PRIMARY KEY,
                    ts TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    title TEXT NOT NULL,
                    predicted_edge REAL NOT NULL,
                    implied_prob REAL,
                    legs TEXT NOT NULL,
                    resolved INTEGER NOT NULL DEFAULT 0,
                    realized_edge REAL,
                    outcome INTEGER
                )
                """
            )

    # -- record ------------------------------------------------------------
    def record(self, opp: Opportunity, prices: dict[str, float]) -> str:
        """Record a flagged opportunity (idempotent by content). Captures the
        per-leg entry price so realized P&L can be computed at resolution."""
        legs = []
        yes_prices = []
        for leg in opp.legs:
            yp = prices.get(leg.market_id, 0.5)
            yes_prices.append(yp)
            entry = round(yp if leg.side == Side.YES else 1.0 - yp, 4)
            # Store venue too, so an autonomous resolver knows which API to ask.
            legs.append({"venue": leg.venue.value, "market_id": leg.market_id,
                         "side": leg.side.value, "entry_price": entry})
        implied = round(sum(yes_prices) / len(yes_prices), 4) if yes_prices else None
        opp_id = _opp_id(opp, legs)
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO flagged "
                "(opp_id, ts, kind, title, predicted_edge, implied_prob, legs) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (opp_id, datetime.now(timezone.utc).isoformat(), opp.kind.value,
                 opp.title, opp.realizable_edge, implied, json.dumps(legs)),
            )
        return opp_id

    # -- resolve -----------------------------------------------------------
    def resolve(self, outcomes: dict[str, int]) -> int:
        """Resolve pending opps whose every leg market has a known outcome (0/1).

        Realized edge = (payoff - cost) / cost over the basket, where a YES leg
        pays 1 if the market resolved YES and a NO leg pays 1 if it resolved NO.
        """
        resolved = 0
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT * FROM flagged WHERE resolved = 0").fetchall()
            for row in rows:
                legs = json.loads(row["legs"])
                if not all(l["market_id"] in outcomes for l in legs):
                    continue
                cost = sum(l["entry_price"] for l in legs)
                payoff = 0.0
                for l in legs:
                    oc = outcomes[l["market_id"]]
                    won = (l["side"] == "yes" and oc == 1) or (l["side"] == "no" and oc == 0)
                    payoff += 1.0 if won else 0.0
                realized = round((payoff - cost) / cost, 4) if cost else 0.0
                rep_outcome = outcomes[legs[0]["market_id"]]
                conn.execute(
                    "UPDATE flagged SET resolved = 1, realized_edge = ?, outcome = ? "
                    "WHERE opp_id = ?",
                    (realized, rep_outcome, row["opp_id"]),
                )
                resolved += 1
        return resolved

    # -- metrics -----------------------------------------------------------
    def metrics(self) -> dict:
        with self._lock, self._connect() as conn:
            pending = conn.execute("SELECT COUNT(*) FROM flagged WHERE resolved = 0").fetchone()[0]
            rows = conn.execute("SELECT * FROM flagged WHERE resolved = 1").fetchall()
        n = len(rows)
        if n == 0:
            return {"resolved_count": 0, "pending_count": pending, "hit_rate": None,
                    "mean_predicted_edge": None, "mean_realized_edge": None,
                    "edge_slippage": None, "brier_score": None}
        hits = sum(1 for r in rows if (r["realized_edge"] or 0) > 0)
        pred = sum(r["predicted_edge"] for r in rows) / n
        real = sum((r["realized_edge"] or 0) for r in rows) / n
        brier_terms = [
            (r["implied_prob"] - r["outcome"]) ** 2
            for r in rows if r["implied_prob"] is not None and r["outcome"] is not None
        ]
        brier = round(sum(brier_terms) / len(brier_terms), 4) if brier_terms else None
        return {
            "resolved_count": n,
            "pending_count": pending,
            "hit_rate": round(hits / n, 4),
            "mean_predicted_edge": round(pred, 4),
            "mean_realized_edge": round(real, 4),
            "edge_slippage": round(pred - real, 4),  # how much rosier we looked than reality
            "brier_score": brier,  # calibration of surfaced implied probs (lower is better)
        }

    def count(self) -> int:
        with self._lock, self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM flagged").fetchone()[0]

    def hit_rate_by_kind(self) -> dict[str, dict]:
        """Per-kind realized hit-rate over resolved records — the ground truth an
        adverse-selection score checks an edge against ('do edges like this win?')."""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT kind, realized_edge FROM flagged WHERE resolved = 1 AND "
                "realized_edge IS NOT NULL").fetchall()
        agg: dict[str, list[int]] = {}
        for r in rows:
            agg.setdefault(r["kind"], []).append(1 if r["realized_edge"] > 0 else 0)
        return {k: {"hit_rate": round(sum(v) / len(v), 4), "samples": len(v)}
                for k, v in agg.items()}

    def pending_legs(self) -> list[tuple[str, str]]:
        """Distinct (venue, market_id) across all UNRESOLVED flags — the set an
        autonomous resolver must check for settlement."""
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT legs FROM flagged WHERE resolved = 0").fetchall()
        seen: set[tuple[str, str]] = set()
        for row in rows:
            for leg in json.loads(row["legs"]):
                venue = leg.get("venue")
                if venue:
                    seen.add((venue, leg["market_id"]))
        return sorted(seen)

    # -- Merkle commitment (Trust v2) -------------------------------------
    def resolved_records(self) -> list[dict]:
        """Resolved entries in a deterministic order (for Merkle commitment)."""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT opp_id, ts, kind, title, predicted_edge, realized_edge, outcome "
                "FROM flagged WHERE resolved = 1 ORDER BY opp_id"
            ).fetchall()
        return [dict(r) for r in rows]

    def calibration_samples(self, category: str | None = None) -> list[tuple[float, int]]:
        """(implied_prob, outcome) pairs from resolved records — the training set
        for probability calibration. Optionally filter by category (matched on the
        title's category is not stored, so this filters by kind-agnostic history)."""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT implied_prob, outcome FROM flagged "
                "WHERE resolved = 1 AND implied_prob IS NOT NULL AND outcome IS NOT NULL"
            ).fetchall()
        return [(float(r["implied_prob"]), int(r["outcome"])) for r in rows]

    def merkle_commitment(self) -> dict:
        """A Merkle root committing to every resolved record, so the published
        track record can't be silently edited after the fact."""
        from .merkle import merkle_root

        records = self.resolved_records()
        return {
            "merkle_root": merkle_root(records),
            "leaf_count": len(records),
            "leaf_domain": "sha256(0x00||canonical_json)",
        }


@lru_cache(maxsize=1)
def get_reconciliation() -> ReconciliationStore:
    return ReconciliationStore()
