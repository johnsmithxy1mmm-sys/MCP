"""House-forecast store — the server's own probability, publicly graded (K1).

The track record (reconciliation) grades the *market's* implied probabilities and
our flagged edges. This grades something stronger: the server's OWN fused forecast
(``houseview.fuse``). Every house probability is recorded here; when the market
resolves it's scored with a Brier score, alongside the market's Brier over the
same set. The killer metric is the difference — ``brier_edge = market_brier -
house_brier`` — the provable claim "our number is better-calibrated than the raw
market," backed by a resolution record no competitor has and cannot fabricate.

One row per market (the latest forecast, upserted until it resolves), so a market
polled many times is graded once, not double-counted. SQLite by default; scoring
is fed by the same autonomous resolver that grows the track record.

  HOUSE_DB_URL   sqlite:///house.db
"""

from __future__ import annotations

import os
import sqlite3
import threading
from functools import lru_cache
from datetime import datetime, timezone
from pathlib import Path


def _sqlite_path(db_url: str) -> str:
    if db_url.startswith("sqlite:///"):
        return db_url[len("sqlite:///"):]
    if db_url.startswith("sqlite://"):
        return db_url[len("sqlite://"):]
    return str(Path.cwd() / "house.db")


class HouseForecastStore:
    def __init__(self, db_url: str | None = None):
        self._url = db_url or os.getenv("HOUSE_DB_URL", "sqlite:///house.db")
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
                CREATE TABLE IF NOT EXISTS house_forecasts (
                    market_id TEXT PRIMARY KEY,
                    venue TEXT,
                    title TEXT,
                    house_prob REAL NOT NULL,
                    market_prob REAL NOT NULL,
                    as_of TEXT NOT NULL,
                    resolved INTEGER NOT NULL DEFAULT 0,
                    outcome INTEGER
                )
                """
            )

    def record(self, market_id: str, venue: str | None, title: str | None,
               house_prob: float, market_prob: float) -> None:
        """Upsert the latest forecast for a market. A resolved row is frozen — a
        later poll must not overwrite a graded forecast (that would let us edit
        history after seeing the outcome)."""
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO house_forecasts
                    (market_id, venue, title, house_prob, market_prob, as_of, resolved)
                VALUES (?, ?, ?, ?, ?, ?, 0)
                ON CONFLICT(market_id) DO UPDATE SET
                    venue=excluded.venue, title=excluded.title,
                    house_prob=excluded.house_prob, market_prob=excluded.market_prob,
                    as_of=excluded.as_of
                WHERE house_forecasts.resolved = 0
                """,
                (market_id, venue, title, round(float(house_prob), 4),
                 round(float(market_prob), 4), datetime.now(timezone.utc).isoformat()),
            )

    def resolve(self, outcomes: dict[str, int]) -> int:
        """Grade pending forecasts whose market has a known outcome (0/1)."""
        resolved = 0
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT market_id FROM house_forecasts WHERE resolved = 0").fetchall()
            for r in rows:
                mid = r["market_id"]
                if mid not in outcomes:
                    continue
                conn.execute(
                    "UPDATE house_forecasts SET resolved = 1, outcome = ? WHERE market_id = ?",
                    (int(outcomes[mid]), mid),
                )
                resolved += 1
        return resolved

    def metrics(self) -> dict:
        """Public house-forecast scorecard: house Brier vs the market's Brier over
        the same resolved set. ``brier_edge`` > 0 means the house beat the market."""
        with self._lock, self._connect() as conn:
            pending = conn.execute(
                "SELECT COUNT(*) FROM house_forecasts WHERE resolved = 0").fetchone()[0]
            rows = conn.execute(
                "SELECT house_prob, market_prob, outcome FROM house_forecasts "
                "WHERE resolved = 1 AND outcome IS NOT NULL").fetchall()
        n = len(rows)
        if n == 0:
            return {"resolved_count": 0, "pending_count": pending,
                    "house_brier": None, "market_brier": None, "brier_edge": None,
                    "house_hit_rate": None}
        house_brier = sum((r["house_prob"] - r["outcome"]) ** 2 for r in rows) / n
        market_brier = sum((r["market_prob"] - r["outcome"]) ** 2 for r in rows) / n
        # A forecast "hits" when it lands on the correct side of 0.5.
        hits = sum(1 for r in rows
                   if (r["house_prob"] >= 0.5) == (r["outcome"] == 1))
        return {
            "resolved_count": n,
            "pending_count": pending,
            "house_brier": round(house_brier, 4),      # lower is better
            "market_brier": round(market_brier, 4),
            "brier_edge": round(market_brier - house_brier, 4),  # >0 => house wins
            "house_hit_rate": round(hits / n, 4),
        }

    def count(self) -> int:
        with self._lock, self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM house_forecasts").fetchone()[0]


@lru_cache(maxsize=1)
def get_house_forecasts() -> HouseForecastStore:
    return HouseForecastStore()
