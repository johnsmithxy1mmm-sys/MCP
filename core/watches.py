"""Watches + alert queue — the push/subscription model.

An agent registers a *watch* (an edge threshold + optional filters); the server
evaluates watches against live opportunities and queues *alerts* when one crosses
the threshold. The agent retrieves fired alerts with a cheap poll instead of
re-running the flagship scanner on a loop.

This changes the economics (billable subscriptions + alerts, not just per-scan)
and the usage model (react to opportunities, don't poll for them). Alerts are
computed on the server (push) and drained by the client (pull) — which works
over stateless MCP and is serverless-friendly, without server-initiated SSE.
# TODO: also emit MCP resource-update notifications for true server push.

SQLite by default (WATCH_DB_URL).
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

from .models import Opportunity


def _sqlite_path(db_url: str) -> str:
    if db_url.startswith("sqlite:///"):
        return db_url[len("sqlite:///"):]
    if db_url.startswith("sqlite://"):
        return db_url[len("sqlite://"):]
    return str(Path.cwd() / "watches.db")


def _opp_sig(opp: Opportunity) -> str:
    key = opp.kind.value + "|" + opp.title + "|" + json.dumps(
        [(l.venue.value, l.market_id, l.side.value) for l in opp.legs], sort_keys=True
    )
    return hashlib.sha256(key.encode()).hexdigest()[:24]


def _opp_payload(opp: Opportunity) -> dict:
    return {
        "kind": opp.kind.value,
        "title": opp.title,
        "category": opp.category,
        "realizable_edge": opp.realizable_edge,
        "annualized_edge": opp.annualized_edge,
        "max_size_usd": opp.max_size_usd,
        "legs": [{"venue": l.venue.value, "market_id": l.market_id, "side": l.side.value} for l in opp.legs],
    }


class WatchStore:
    def __init__(self, db_url: str | None = None):
        self._url = db_url or os.getenv("WATCH_DB_URL", "sqlite:///watches.db")
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
                CREATE TABLE IF NOT EXISTS watches (
                    watch_id TEXT PRIMARY KEY,
                    client_id TEXT NOT NULL,
                    min_edge REAL NOT NULL,
                    category TEXT,
                    kind TEXT,
                    event TEXT,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS alerts (
                    watch_id TEXT NOT NULL,
                    opp_sig TEXT NOT NULL,
                    client_id TEXT NOT NULL,
                    ts TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    delivered INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (watch_id, opp_sig)
                )
                """
            )

    # -- watches -----------------------------------------------------------
    def create_watch(
        self, client_id: str, min_edge: float,
        category: str | None = None, kind: str | None = None, event: str | None = None,
    ) -> str:
        watch_id = "w_" + uuid.uuid4().hex[:12]
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO watches (watch_id, client_id, min_edge, category, kind, event, active, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 1, ?)",
                (watch_id, client_id, min_edge, category, kind, event,
                 datetime.now(timezone.utc).isoformat()),
            )
        return watch_id

    def list_watches(self, client_id: str) -> list[dict]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM watches WHERE client_id = ? AND active = 1 ORDER BY created_at",
                (client_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def cancel_watch(self, client_id: str, watch_id: str) -> bool:
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE watches SET active = 0 WHERE watch_id = ? AND client_id = ?",
                (watch_id, client_id),
            )
            return cur.rowcount > 0

    # -- fire + drain ------------------------------------------------------
    def fire(self, opps: list[Opportunity]) -> int:
        """Evaluate all active watches against `opps`, queue new matching alerts.

        Deduped by (watch_id, opp signature) so a standing opportunity alerts once.
        Returns the number of new alerts queued.
        """
        fired = 0
        with self._lock, self._connect() as conn:
            watches = conn.execute("SELECT * FROM watches WHERE active = 1").fetchall()
            for w in watches:
                for opp in opps:
                    if opp.realizable_edge < w["min_edge"]:
                        continue
                    if w["category"] and (opp.category or "") != w["category"]:
                        continue
                    if w["kind"] and opp.kind.value != w["kind"]:
                        continue
                    if w["event"] and w["event"].lower() not in opp.title.lower():
                        continue
                    cur = conn.execute(
                        "INSERT OR IGNORE INTO alerts (watch_id, opp_sig, client_id, ts, payload) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (w["watch_id"], _opp_sig(opp), w["client_id"],
                         datetime.now(timezone.utc).isoformat(), json.dumps(_opp_payload(opp))),
                    )
                    fired += cur.rowcount
        return fired

    def drain(self, client_id: str) -> list[dict]:
        """Return this client's undelivered alerts and mark them delivered."""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT watch_id, opp_sig, ts, payload FROM alerts "
                "WHERE client_id = ? AND delivered = 0 ORDER BY ts",
                (client_id,),
            ).fetchall()
            if rows:
                conn.execute(
                    "UPDATE alerts SET delivered = 1 WHERE client_id = ? AND delivered = 0",
                    (client_id,),
                )
        return [
            {"watch_id": r["watch_id"], "ts": r["ts"], "opportunity": json.loads(r["payload"])}
            for r in rows
        ]


@lru_cache(maxsize=1)
def get_watches() -> WatchStore:
    return WatchStore()
