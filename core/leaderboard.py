"""Agent leaderboard — proof-of-alpha as a service (K4).

Every feature so far helps an agent *find* edge. K4 lets an agent *prove* it. When
a caller commits an intended position (``estimate_execution(commit=true)``) the
legs and entry prices are logged as a paper trade tagged to that caller; when the
markets resolve, the P&L is scored exactly like the server's own track record.
The aggregate is a public, server-signed leaderboard of agents ranked by REAL,
resolution-verified return — so an agent can point a third party (an investor, an
employer, another agent) at an independent notary of its alpha instead of
self-reporting. That's the network effect the product was missing: agents come not
only for data but to build a portable, tamper-evident performance record here.

One paper trade per commit; graded by the same autonomous resolver that grows the
track record. Public rows are anonymized to a stable hash handle (never a raw
client id / bearer-token hash); a caller sees its own detailed portfolio via the
ownership-checked ``portfolio://`` resource. SQLite by default.

  LEADERBOARD_DB_URL       sqlite:///leaderboard.db
  LEADERBOARD_MIN_RESOLVED resolved trades before an agent is ranked (default 3)
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path


def _sqlite_path(db_url: str) -> str:
    if db_url.startswith("sqlite:///"):
        return db_url[len("sqlite:///"):]
    if db_url.startswith("sqlite://"):
        return db_url[len("sqlite://"):]
    return str(Path.cwd() / "leaderboard.db")


def handle_for(client_id: str) -> str:
    """Stable, anonymous public handle for a caller — never the raw id."""
    return "agent-" + hashlib.sha256(client_id.encode()).hexdigest()[:10]


class PaperTradeStore:
    def __init__(self, db_url: str | None = None):
        self._url = db_url or os.getenv("LEADERBOARD_DB_URL", "sqlite:///leaderboard.db")
        self._path = _sqlite_path(self._url)
        self._lock = threading.Lock()
        self._min_resolved = int(os.getenv("LEADERBOARD_MIN_RESOLVED", "3"))
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS paper_trades (
                    trade_id TEXT PRIMARY KEY,
                    client_id TEXT NOT NULL,
                    ts TEXT NOT NULL,
                    title TEXT,
                    legs TEXT NOT NULL,
                    size_usd REAL NOT NULL,
                    resolved INTEGER NOT NULL DEFAULT 0,
                    realized_edge REAL,
                    pnl_usd REAL,
                    outcome INTEGER
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_pt_client ON paper_trades (client_id)")

    # -- record -----------------------------------------------------------
    def record(self, client_id: str, size_usd: float,
               legs: list[dict], title: str | None = None) -> str:
        """Log a committed paper trade. ``legs`` carry a yes-equivalent
        ``entry_price`` each, so P&L can be computed at resolution."""
        trade_id = "t_" + uuid.uuid4().hex[:12]
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO paper_trades (trade_id, client_id, ts, title, legs, size_usd) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (trade_id, client_id, datetime.now(timezone.utc).isoformat(),
                 title, json.dumps(legs), round(float(size_usd), 2)),
            )
        return trade_id

    # -- resolve ----------------------------------------------------------
    def resolve(self, outcomes: dict[str, int]) -> int:
        """Grade pending trades whose every leg market has settled.

        Realized edge = (payoff − cost) / cost over the basket (a YES leg pays 1 on
        YES, a NO leg pays 1 on NO); paper P&L = size × realized edge — identical to
        the reconciliation model, so the leaderboard and track record agree.
        """
        resolved = 0
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT * FROM paper_trades WHERE resolved = 0").fetchall()
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
                pnl = round(row["size_usd"] * realized, 2)
                conn.execute(
                    "UPDATE paper_trades SET resolved = 1, realized_edge = ?, pnl_usd = ?, "
                    "outcome = ? WHERE trade_id = ?",
                    (realized, pnl, outcomes[legs[0]["market_id"]], row["trade_id"]),
                )
                resolved += 1
        return resolved

    # -- aggregates -------------------------------------------------------
    def _client_rows(self) -> dict[str, list[sqlite3.Row]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT * FROM paper_trades").fetchall()
        by_client: dict[str, list] = {}
        for r in rows:
            by_client.setdefault(r["client_id"], []).append(r)
        return by_client

    @staticmethod
    def _stats(rows: list[sqlite3.Row]) -> dict:
        resolved = [r for r in rows if r["resolved"] == 1]
        n = len(resolved)
        total_pnl = round(sum(r["pnl_usd"] or 0 for r in resolved), 2)
        deployed = round(sum(r["size_usd"] for r in resolved), 2)
        roi = round(total_pnl / deployed, 4) if deployed else 0.0
        hits = sum(1 for r in resolved if (r["realized_edge"] or 0) > 0)
        return {
            "trades": len(rows),
            "resolved_trades": n,
            "pending_trades": len(rows) - n,
            "total_pnl_usd": total_pnl,
            "deployed_usd": deployed,
            "roi": roi,
            "hit_rate": round(hits / n, 4) if n else None,
        }

    def leaderboard(self, limit: int = 20) -> dict:
        """Public ranking by realized P&L. Agents with fewer than
        ``LEADERBOARD_MIN_RESOLVED`` resolved trades are listed as provisional
        (shown but unranked), so a single lucky trade can't top a proven record."""
        ranked: list[dict] = []
        provisional: list[dict] = []
        for client_id, rows in self._client_rows().items():
            stats = self._stats(rows)
            entry = {"handle": handle_for(client_id), **stats}
            (ranked if stats["resolved_trades"] >= self._min_resolved
             else provisional).append(entry)
        ranked.sort(key=lambda e: e["total_pnl_usd"], reverse=True)
        for i, e in enumerate(ranked, 1):
            e["rank"] = i
        return {
            "ranked": ranked[:limit],
            "provisional": len(provisional),
            "agents": len(ranked) + len(provisional),
            "min_resolved_to_rank": self._min_resolved,
            "method": "ranked by realized paper P&L over resolution-verified trades",
        }

    def client_summary(self, client_id: str) -> dict:
        """A caller's OWN detailed paper portfolio + its leaderboard rank."""
        rows = self._client_rows().get(client_id, [])
        stats = self._stats(rows)
        rank = None
        if stats["resolved_trades"] >= self._min_resolved:
            board = self.leaderboard(limit=10_000)["ranked"]
            mine = handle_for(client_id)
            rank = next((e["rank"] for e in board if e["handle"] == mine), None)
        trades = [
            {"trade_id": r["trade_id"], "ts": r["ts"], "title": r["title"],
             "size_usd": r["size_usd"], "resolved": bool(r["resolved"]),
             "realized_edge": r["realized_edge"], "pnl_usd": r["pnl_usd"],
             "legs": json.loads(r["legs"])}
            for r in sorted(rows, key=lambda r: r["ts"], reverse=True)
        ]
        return {"handle": handle_for(client_id), "rank": rank, **stats, "trades": trades}

    def count(self) -> int:
        with self._lock, self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0]


@lru_cache(maxsize=1)
def get_leaderboard() -> PaperTradeStore:
    return PaperTradeStore()
