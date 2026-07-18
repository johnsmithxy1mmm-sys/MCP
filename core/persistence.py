"""Edge-survival model — rank by reachable value, not paper edge (F2).

A 4% edge that vanishes in 30 seconds is worth less than a 2% edge that lasts an
hour, but every scanner on the market sorts by edge magnitude alone. This module
learns the difference from the server's own scan history: each pass it notes
which opportunities are still present and which have disappeared, accumulating
the empirical *lifespan* of opportunities by (kind, category). From that it
estimates the probability an opportunity survives the execution window, and turns
raw edge into an **expected value** — the honest ranking key.

Pure bookkeeping over SQLite; no external deps. The estimate is None until enough
lifespans are observed, so we never fake a survival number on thin evidence.

  PERSISTENCE_DB_URL       sqlite:///persistence.db
  PERSISTENCE_MIN_SAMPLES  observed lifespans required per bucket (default 20)
  EXEC_HORIZON_SECONDS     execution window survival is measured against (default 60)
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import threading
import time
from functools import lru_cache
from pathlib import Path


def _sqlite_path(db_url: str) -> str:
    if db_url.startswith("sqlite:///"):
        return db_url[len("sqlite:///"):]
    if db_url.startswith("sqlite://"):
        return db_url[len("sqlite://"):]
    return str(Path.cwd() / "persistence.db")


def opp_signature(opp) -> str:
    """Stable id for an opportunity across scans (kind + title + legs)."""
    import json

    key = opp.kind.value + "|" + opp.title + "|" + json.dumps(
        sorted((l.venue.value, l.market_id, l.side.value) for l in opp.legs))
    return hashlib.sha256(key.encode()).hexdigest()[:24]


def _bucket(opp) -> str:
    return f"{opp.kind.value}|{opp.category or ''}"


class PersistenceStore:
    def __init__(self, db_url: str | None = None):
        self._url = db_url or os.getenv("PERSISTENCE_DB_URL", "sqlite:///persistence.db")
        self._path = _sqlite_path(self._url)
        self._lock = threading.Lock()
        self._min_samples = int(os.getenv("PERSISTENCE_MIN_SAMPLES", "20"))
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS sightings ("
                "  sig TEXT PRIMARY KEY, bucket TEXT NOT NULL,"
                "  first_seen REAL NOT NULL, last_seen REAL NOT NULL)")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS lifespans ("
                "  id INTEGER PRIMARY KEY AUTOINCREMENT, bucket TEXT NOT NULL, seconds REAL NOT NULL)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_life_bucket ON lifespans (bucket)")

    def observe(self, opps: list) -> None:
        """Record the current opportunity set. Opportunities present last time but
        gone now have their lifespan closed out into the bucket stats.

        Must be called on the FULL (unfiltered) opp set, or a filtered-out
        opportunity would be misread as 'disappeared'.
        """
        now = time.time()
        current = {opp_signature(o): _bucket(o) for o in opps}
        with self._lock, self._connect() as conn:
            prior = {r["sig"]: r for r in conn.execute("SELECT * FROM sightings").fetchall()}
            # Disappeared -> close out the lifespan.
            for sig, row in prior.items():
                if sig not in current:
                    conn.execute("INSERT INTO lifespans (bucket, seconds) VALUES (?, ?)",
                                 (row["bucket"], max(0.0, row["last_seen"] - row["first_seen"])))
                    conn.execute("DELETE FROM sightings WHERE sig = ?", (sig,))
            # Present -> upsert (new ones start their clock now).
            for sig, bucket in current.items():
                if sig in prior:
                    conn.execute("UPDATE sightings SET last_seen = ? WHERE sig = ?", (now, sig))
                else:
                    conn.execute(
                        "INSERT INTO sightings (sig, bucket, first_seen, last_seen) VALUES (?, ?, ?, ?)",
                        (sig, bucket, now, now))

    def survival(self, bucket: str, horizon_seconds: float | None = None) -> float | None:
        """Empirical P(lifespan >= horizon) for a bucket, or None if too few
        samples. Laplace-smoothed so it never returns a hard 0 or 1."""
        horizon = horizon_seconds if horizon_seconds is not None else float(
            os.getenv("EXEC_HORIZON_SECONDS", "60"))
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT seconds FROM lifespans WHERE bucket = ?", (bucket,)).fetchall()
        spans = [r["seconds"] for r in rows]
        if len(spans) < self._min_samples:
            return None
        survived = sum(1 for s in spans if s >= horizon)
        return round((survived + 1) / (len(spans) + 2), 4)  # Laplace smoothing

    def median_lifespan(self, bucket: str) -> float | None:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT seconds FROM lifespans WHERE bucket = ? ORDER BY seconds", (bucket,)).fetchall()
        spans = [r["seconds"] for r in rows]
        if not spans:
            return None
        mid = len(spans) // 2
        return round(spans[mid] if len(spans) % 2 else (spans[mid - 1] + spans[mid]) / 2, 1)

    def annotate(self, opps: list, horizon_seconds: float | None = None) -> list:
        """Attach survival_probability + expected_value and re-rank by EV.

        expected_value = realizable_edge * survival (or the raw edge when survival
        is still unknown — no penalty on thin evidence).
        """
        for opp in opps:
            sp = self.survival(_bucket(opp), horizon_seconds)
            opp.survival_probability = sp
            opp.expected_value = round(opp.realizable_edge * (sp if sp is not None else 1.0), 4)
        opps.sort(key=lambda o: (o.expected_value if o.expected_value is not None
                                 else o.realizable_edge), reverse=True)
        return opps


@lru_cache(maxsize=1)
def get_persistence() -> PersistenceStore:
    return PersistenceStore()
