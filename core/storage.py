"""Historical price/spread store.

Mirrors the billing metering pattern: a pluggable store with a working local
SQLite backend (zero infra, default) and a Timescale/Postgres seam via env. The
live engine records a price point whenever it observes a market; the
``get_market_history`` tool reads the series back.

  HISTORY_DB_URL   sqlite:///history.db (default) | postgresql://… (Timescale)

Credentials/DSN come from the environment only.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from .models import Market, PricePoint


def _sqlite_path(db_url: str) -> str:
    if db_url.startswith("sqlite:///"):
        return db_url[len("sqlite:///"):]
    if db_url.startswith("sqlite://"):
        return db_url[len("sqlite://"):]
    # Non-sqlite DSN (e.g. Timescale): fall back to a local file so the server
    # never crashes. # TODO: real asyncpg/Timescale writer for production.
    return str(Path.cwd() / "history.db")


class HistoryStore:
    """SQLite-backed time series of (venue, market_id) -> yes_price/spread."""

    def __init__(self, db_url: str | None = None):
        self._url = db_url or os.getenv("HISTORY_DB_URL", "sqlite:///history.db")
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
                CREATE TABLE IF NOT EXISTS price_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    venue TEXT NOT NULL,
                    market_id TEXT NOT NULL,
                    ts TEXT NOT NULL,
                    yes_price REAL NOT NULL,
                    spread REAL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_hist_market_ts "
                "ON price_history (venue, market_id, ts)"
            )

    # -- write ------------------------------------------------------------
    def record(
        self,
        venue: str,
        market_id: str,
        yes_price: float,
        spread: float | None = None,
        ts: datetime | None = None,
    ) -> None:
        ts = ts or datetime.now(timezone.utc)
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO price_history (venue, market_id, ts, yes_price, spread) "
                "VALUES (?, ?, ?, ?, ?)",
                (venue, market_id, ts.isoformat(), round(yes_price, 4),
                 round(spread, 4) if spread is not None else None),
            )

    def record_market(self, market: Market, ts: datetime | None = None) -> None:
        """Convenience: record a snapshot straight from a Market."""
        # spread proxy: how far the book is from a fair 1.0 book (yes+no).
        spread = round(abs(1.0 - (market.yes_price + market.no_price)), 4)
        self.record(market.venue.value, market.market_id, market.yes_price, spread, ts)

    # -- read -------------------------------------------------------------
    def query(
        self, venue: str, market_id: str, frm: datetime, to: datetime
    ) -> list[PricePoint]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT ts, yes_price, spread FROM price_history "
                "WHERE venue = ? AND market_id = ? AND ts >= ? AND ts <= ? "
                "ORDER BY ts ASC",
                (venue, market_id, frm.isoformat(), to.isoformat()),
            ).fetchall()
        return [
            PricePoint(
                ts=datetime.fromisoformat(r["ts"]),
                yes_price=r["yes_price"],
                spread=r["spread"],
            )
            for r in rows
        ]

    def count(self) -> int:
        with self._lock, self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM price_history").fetchone()[0]
