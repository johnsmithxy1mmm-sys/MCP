"""D2: asyncpg Timescale/Postgres history path (fake pool, no live DB)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


from core import storage
from core.pg import AsyncLoop, get_loop, is_postgres_dsn
from core.storage import HistoryStore, PgHistoryStore, _SqliteHistoryStore


# --- DSN detection + selection ----------------------------------------------
def test_is_postgres_dsn():
    assert is_postgres_dsn("postgresql://u@h/db")
    assert is_postgres_dsn("postgres://u@h/db")
    assert not is_postgres_dsn("sqlite:///x.db")


def test_factory_returns_sqlite_for_sqlite(tmp_path):
    store = HistoryStore(f"sqlite:///{tmp_path / 'h.db'}")
    assert isinstance(store, _SqliteHistoryStore)


def test_factory_falls_back_to_sqlite_on_pg_failure(monkeypatch):
    def boom(dsn, loop=None, pool=None):
        raise RuntimeError("no driver / DB unreachable")

    monkeypatch.setattr(storage, "PgHistoryStore", boom)
    store = HistoryStore("postgresql://u@h/db")
    assert isinstance(store, _SqliteHistoryStore)  # degraded, never crashes


# --- AsyncLoop --------------------------------------------------------------
def test_async_loop_runs_coroutines():
    loop = AsyncLoop()

    async def add(a, b):
        return a + b

    assert loop.run(add(2, 3)) == 5


# --- PgHistoryStore over a fake asyncpg pool --------------------------------
class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *a):
        return False


class FakeConn:
    def __init__(self, state):
        self.state = state

    async def execute(self, sql, *args):
        s = sql.strip().upper()
        if s.startswith("INSERT"):
            venue, market_id, ts, yes_price, spread = args
            self.state["rows"].append(
                {"venue": venue, "market_id": market_id, "ts": ts,
                 "yes_price": yes_price, "spread": spread})
        elif s.startswith("DELETE"):
            (cutoff,) = args
            self.state["rows"] = [r for r in self.state["rows"] if r["ts"] >= cutoff]
        # CREATE TABLE / INDEX: no-op

    async def fetch(self, sql, *args):
        venue, market_id, frm, to = args
        return [r for r in self.state["rows"]
                if r["venue"] == venue and r["market_id"] == market_id
                and frm <= r["ts"] <= to]

    async def fetchval(self, sql, *args):
        return len(self.state["rows"])


class FakePgPool:
    def __init__(self):
        self.state = {"rows": []}

    def acquire(self):
        return _Acquire(FakeConn(self.state))


def test_pg_history_store_roundtrip():
    pool = FakePgPool()
    store = PgHistoryStore("postgresql://u@h/db", loop=get_loop(), pool=pool)
    now = datetime.now(timezone.utc)
    store.record("kalshi", "m1", 0.62, spread=0.01, ts=now)
    store.record("kalshi", "m1", 0.64, ts=now + timedelta(minutes=1))
    store.record("polymarket", "other", 0.5, ts=now)

    pts = store.query("kalshi", "m1", now - timedelta(hours=1), now + timedelta(hours=1))
    assert [p.yes_price for p in pts] == [0.62, 0.64]  # ordered, filtered by market
    assert store.count() == 3


def test_pg_history_store_record_market():
    from core.models import Market, Venue

    pool = FakePgPool()
    store = PgHistoryStore("postgresql://u@h/db", loop=get_loop(), pool=pool)
    m = Market(venue=Venue.KALSHI, market_id="kx", title="t", yes_price=0.7, no_price=0.3)
    store.record_market(m)
    assert store.count() == 1
    assert pool.state["rows"][0]["yes_price"] == 0.7
