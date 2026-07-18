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
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .models import Market, PricePoint


def _sqlite_path(db_url: str) -> str:
    if db_url.startswith("sqlite:///"):
        return db_url[len("sqlite:///"):]
    if db_url.startswith("sqlite://"):
        return db_url[len("sqlite://"):]
    # Non-sqlite DSN that isn't Postgres: fall back to a local file so the server
    # never crashes (Postgres DSNs are handled by PgHistoryStore).
    return str(Path.cwd() / "history.db")


class _SqliteHistoryStore:
    """SQLite-backed time series of (venue, market_id) -> yes_price/spread."""

    def __init__(self, db_url: str | None = None):
        self._url = db_url or os.getenv("HISTORY_DB_URL", "sqlite:///history.db")
        self._path = _sqlite_path(self._url)
        self._lock = threading.Lock()
        # Bounded retention so the local store can't grow forever. 0/negative
        # disables pruning (keep everything). Checked at most once per hour.
        self._retention_days = float(os.getenv("HISTORY_RETENTION_DAYS", "90"))
        self._last_prune: float | None = None  # None -> prune on first write
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
        self._maybe_prune()  # opportunistic, throttled; before we take the lock
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO price_history (venue, market_id, ts, yes_price, spread) "
                "VALUES (?, ?, ?, ?, ?)",
                (venue, market_id, ts.isoformat(), round(yes_price, 4),
                 round(spread, 4) if spread is not None else None),
            )

    def _maybe_prune(self) -> None:
        """Delete points older than the retention window (throttled to hourly)."""
        if self._retention_days <= 0:
            return
        now = time.monotonic()
        # None-sentinel (not 0.0): monotonic() starts near zero on fresh boots,
        # which would silently skip pruning for the machine's first hour.
        if self._last_prune is not None and now - self._last_prune < 3600:
            return
        self._last_prune = now
        cutoff = (datetime.now(timezone.utc) - timedelta(days=self._retention_days)).isoformat()
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM price_history WHERE ts < ?", (cutoff,))

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


class PgHistoryStore:
    """Postgres/Timescale-backed history store (same surface as the SQLite one).

    Runs asyncpg through the shared :class:`~core.pg.AsyncLoop` so the sync
    callers are unchanged. The table is a plain relational table that a Timescale
    deployment can turn into a hypertable; server-side retention is a single
    ``DELETE ... WHERE ts < now() - interval``.
    """

    def __init__(self, dsn: str, loop=None, pool=None):
        from .pg import get_loop

        self._dsn = dsn
        self._loop = loop or get_loop()
        self._pool = pool  # injectable for tests
        self._retention_days = float(os.getenv("HISTORY_RETENTION_DAYS", "90"))
        self._last_prune: float | None = None
        self._loop.run(self._ensure_schema())

    async def _pool_ready(self):
        if self._pool is None:
            from .pg import make_pool

            self._pool = await make_pool(self._dsn)
        return self._pool

    async def _ensure_schema(self) -> None:
        pool = await self._pool_ready()
        async with pool.acquire() as conn:
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS price_history ("
                "  id BIGSERIAL PRIMARY KEY, venue TEXT NOT NULL, market_id TEXT NOT NULL,"
                "  ts TIMESTAMPTZ NOT NULL, yes_price DOUBLE PRECISION NOT NULL,"
                "  spread DOUBLE PRECISION)"
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_hist_market_ts "
                "ON price_history (venue, market_id, ts)"
            )

    def record(self, venue, market_id, yes_price, spread=None, ts=None) -> None:
        ts = ts or datetime.now(timezone.utc)
        self._maybe_prune()
        self._loop.run(self._record(venue, market_id, round(yes_price, 4),
                                    round(spread, 4) if spread is not None else None, ts))

    async def _record(self, venue, market_id, yes_price, spread, ts) -> None:
        pool = await self._pool_ready()
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO price_history (venue, market_id, ts, yes_price, spread) "
                "VALUES ($1, $2, $3, $4, $5)",
                venue, market_id, ts, yes_price, spread,
            )

    def record_market(self, market: Market, ts: datetime | None = None) -> None:
        spread = round(abs(1.0 - (market.yes_price + market.no_price)), 4)
        self.record(market.venue.value, market.market_id, market.yes_price, spread, ts)

    def _maybe_prune(self) -> None:
        if self._retention_days <= 0:
            return
        now = time.monotonic()
        if self._last_prune is not None and now - self._last_prune < 3600:
            return
        self._last_prune = now
        cutoff = datetime.now(timezone.utc) - timedelta(days=self._retention_days)
        self._loop.run(self._prune(cutoff))

    async def _prune(self, cutoff) -> None:
        pool = await self._pool_ready()
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM price_history WHERE ts < $1", cutoff)

    def query(self, venue, market_id, frm, to) -> list[PricePoint]:
        rows = self._loop.run(self._query(venue, market_id, frm, to))
        return [PricePoint(ts=r["ts"], yes_price=r["yes_price"], spread=r["spread"]) for r in rows]

    async def _query(self, venue, market_id, frm, to):
        pool = await self._pool_ready()
        async with pool.acquire() as conn:
            return await conn.fetch(
                "SELECT ts, yes_price, spread FROM price_history "
                "WHERE venue = $1 AND market_id = $2 AND ts >= $3 AND ts <= $4 ORDER BY ts ASC",
                venue, market_id, frm, to,
            )

    def count(self) -> int:
        return self._loop.run(self._count())

    async def _count(self) -> int:
        pool = await self._pool_ready()
        async with pool.acquire() as conn:
            return await conn.fetchval("SELECT COUNT(*) FROM price_history")


def HistoryStore(db_url: str | None = None):
    """Return the right history store for the configured DSN.

    ``postgresql://`` / ``postgres://`` -> :class:`PgHistoryStore` (Timescale
    path); anything else -> the zero-infra SQLite store. On any Postgres setup
    failure we degrade to SQLite so the server always starts.
    """
    from .pg import is_postgres_dsn

    url = db_url or os.getenv("HISTORY_DB_URL", "sqlite:///history.db")
    if is_postgres_dsn(url):
        try:
            return PgHistoryStore(url)
        except Exception:  # driver missing / DB unreachable -> local fallback
            import logging

            logging.getLogger("predmarket.storage").warning(
                "Postgres history store unavailable; falling back to SQLite"
            )
    return _SqliteHistoryStore(db_url)
