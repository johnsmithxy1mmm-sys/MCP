"""asyncpg bridge for the synchronous stores (D2).

Our stores (history, metering) are synchronous and are called both directly and
from worker threads. asyncpg is async-only, so this module runs a single asyncio
loop on a dedicated daemon thread and lets sync code call coroutines through it
(``AsyncLoop.run``). That gives us a real Timescale/Postgres path — a connection
pool, server-side time bucketing — without rewriting every caller as async.

Selection is by DSN: ``postgresql://`` / ``postgres://`` engages this path; the
SQLite default is untouched. If asyncpg or the DB is unavailable the caller keeps
its local SQLite file, so nothing hard-fails.

  HISTORY_DB_URL   postgresql://user:pass@host/db   (Timescale hypertable ok)
  PG_POOL_MIN / PG_POOL_MAX   pool bounds (default 1 / 10)
"""

from __future__ import annotations

import asyncio
import os
import threading
from functools import lru_cache


def is_postgres_dsn(dsn: str) -> bool:
    return dsn.startswith("postgresql://") or dsn.startswith("postgres://")


class AsyncLoop:
    """A private asyncio event loop running on a daemon thread.

    ``run(coro)`` submits the coroutine to that loop and blocks for its result,
    so synchronous code can drive asyncpg safely from any thread.
    """

    def __init__(self):
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_forever, name="pg-loop", daemon=True)
        self._thread.start()

    def _run_forever(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def run(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result()


@lru_cache(maxsize=1)
def get_loop() -> AsyncLoop:
    return AsyncLoop()


async def make_pool(dsn: str):
    """Create an asyncpg pool (imported lazily so the dep stays optional)."""
    import asyncpg

    return await asyncpg.create_pool(
        dsn,
        min_size=int(os.getenv("PG_POOL_MIN", "1")),
        max_size=int(os.getenv("PG_POOL_MAX", "10")),
    )
