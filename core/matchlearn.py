"""Self-learning matcher — calibrate match confidence from ground truth (H5).

The cross-venue matcher decides "these two markets are the same event" from
lexical/semantic similarity. But we eventually learn the truth: once both markets
resolve, a genuine match resolves to the SAME outcome; a false match diverges.
This module records every surfaced match and, at resolution, whether the two
agreed — then calibrates raw similarity into an empirical **true-match
probability**. The matcher literally learns from ground truth: a 0.6 similarity
that historically agreed 90% of the time is worth more than the raw score says.

Reuses the C3 isotonic Calibrator (a match observation is exactly a
(score, hit) pair). Identity until ``MATCHLEARN_MIN_SAMPLES`` resolved matches —
we never re-weight confidence on thin evidence. SQLite-backed, offline.

  MATCHLEARN_DB_URL        sqlite:///matchlearn.db
  MATCHLEARN_MIN_SAMPLES   resolved matches required before calibrating (default 20)
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import threading
from functools import lru_cache
from pathlib import Path

from .calibration import Calibrator, _pav


def _sqlite_path(db_url: str) -> str:
    if db_url.startswith("sqlite:///"):
        return db_url[len("sqlite:///"):]
    if db_url.startswith("sqlite://"):
        return db_url[len("sqlite://"):]
    return str(Path.cwd() / "matchlearn.db")


def _pair_id(a: str, b: str) -> str:
    key = "|".join(sorted((a, b)))
    return hashlib.sha256(key.encode()).hexdigest()[:24]


class MatchLearnStore:
    def __init__(self, db_url: str | None = None):
        self._url = db_url or os.getenv("MATCHLEARN_DB_URL", "sqlite:///matchlearn.db")
        self._path = _sqlite_path(self._url)
        self._lock = threading.Lock()
        self._min_samples = int(os.getenv("MATCHLEARN_MIN_SAMPLES", "20"))
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS match_obs ("
                "  pair_id TEXT PRIMARY KEY, a TEXT NOT NULL, b TEXT NOT NULL,"
                "  confidence REAL NOT NULL, resolved INTEGER NOT NULL DEFAULT 0,"
                "  agreed INTEGER)")

    def record(self, a: str, b: str, confidence: float) -> None:
        """Note a surfaced match (idempotent by pair)."""
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO match_obs (pair_id, a, b, confidence) VALUES (?,?,?,?)",
                (_pair_id(a, b), a, b, round(confidence, 4)))

    def resolve(self, outcomes: dict[str, int]) -> int:
        """Close out matches whose BOTH markets have settled: agreed iff same
        outcome. Returns the number newly resolved."""
        n = 0
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT * FROM match_obs WHERE resolved = 0").fetchall()
            for r in rows:
                if r["a"] in outcomes and r["b"] in outcomes:
                    agreed = int(outcomes[r["a"]] == outcomes[r["b"]])
                    conn.execute("UPDATE match_obs SET resolved = 1, agreed = ? WHERE pair_id = ?",
                                 (agreed, r["pair_id"]))
                    n += 1
        return n

    def samples(self) -> list[tuple[float, int]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT confidence, agreed FROM match_obs WHERE resolved = 1 AND agreed IS NOT NULL"
            ).fetchall()
        return [(float(r["confidence"]), int(r["agreed"])) for r in rows]

    def _calibrator(self) -> Calibrator:
        samples = self.samples()
        if len(samples) < self._min_samples:
            return Calibrator([], [], len(samples))
        n_bins = max(2, int(os.getenv("MATCHLEARN_BINS", "10")))
        sums = [0.0] * n_bins
        counts = [0] * n_bins
        for conf, agreed in samples:
            idx = min(n_bins - 1, max(0, int(conf * n_bins)))
            sums[idx] += agreed
            counts[idx] += 1
        centers, rates, weights = [], [], []
        for i in range(n_bins):
            if counts[i]:
                centers.append((i + 0.5) / n_bins)
                rates.append(sums[i] / counts[i])
                weights.append(float(counts[i]))
        if len(centers) < 2:
            return Calibrator([], [], len(samples))
        return Calibrator(centers, _pav(rates, weights), len(samples))

    def calibrate(self, confidence: float) -> dict:
        """Raw similarity -> empirical true-match probability (+ how much history
        informed it). Identity until enough resolved matches exist."""
        cal = self._calibrator()
        return {
            "raw_confidence": round(confidence, 4),
            "calibrated_confidence": cal.calibrate(confidence),
            "samples": cal.n,
            "learned": not cal.is_identity,
        }


@lru_cache(maxsize=1)
def get_matchlearn() -> MatchLearnStore:
    return MatchLearnStore()
