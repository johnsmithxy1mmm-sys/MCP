"""Calibration-weighted meta-consensus (J4).

Fair value (B3) fuses venue prices by liquidity. But liquidity isn't accuracy —
a deep venue can still be systematically wrong. Meta-consensus weights each venue
by how ACCURATE it has actually been: every time a market resolves we score the
venue's surfaced price against the outcome (Brier), and a venue that's
historically closer to truth gets more say. The result is a best-estimate plus a
credible interval — 'the market's true probability, weighted by who's usually
right'. It's meta-calibration: the moat compounds with resolution history and
can't be copied without it.

Neutral (equal) weighting until ``ACCURACY_MIN_SAMPLES`` resolved samples per
venue, so we never over-trust a venue on thin evidence. SQLite-backed, offline.

  ACCURACY_DB_URL        sqlite:///accuracy.db
  ACCURACY_MIN_SAMPLES   resolved samples per venue before weighting (default 20)
"""

from __future__ import annotations

import os
import sqlite3
import threading
from functools import lru_cache
from pathlib import Path


def _sqlite_path(db_url: str) -> str:
    if db_url.startswith("sqlite:///"):
        return db_url[len("sqlite:///"):]
    if db_url.startswith("sqlite://"):
        return db_url[len("sqlite://"):]
    return str(Path.cwd() / "accuracy.db")


class VenueAccuracyStore:
    def __init__(self, db_url: str | None = None):
        self._url = db_url or os.getenv("ACCURACY_DB_URL", "sqlite:///accuracy.db")
        self._path = _sqlite_path(self._url)
        self._lock = threading.Lock()
        self._min = int(os.getenv("ACCURACY_MIN_SAMPLES", "20"))
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS venue_samples ("
                         "  id INTEGER PRIMARY KEY AUTOINCREMENT, venue TEXT NOT NULL,"
                         "  price REAL NOT NULL, outcome INTEGER NOT NULL)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_vs_venue ON venue_samples (venue)")

    def record(self, venue: str, yes_price: float, outcome: int) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("INSERT INTO venue_samples (venue, price, outcome) VALUES (?,?,?)",
                         (venue, round(float(yes_price), 4), int(outcome)))

    def accuracy(self, venue: str) -> dict:
        """Venue weight in [0,1] = 1 - Brier over its resolved samples. Neutral
        (0.5) until enough evidence — lower Brier (better calibration) => higher
        weight."""
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT price, outcome FROM venue_samples WHERE venue = ?",
                                (venue,)).fetchall()
        n = len(rows)
        if n < self._min:
            return {"weight": 0.5, "samples": n, "calibrated": False}
        brier = sum((r["price"] - r["outcome"]) ** 2 for r in rows) / n
        return {"weight": round(max(0.0, 1.0 - brier), 4), "brier": round(brier, 4),
                "samples": n, "calibrated": True}


@lru_cache(maxsize=1)
def get_venue_accuracy() -> VenueAccuracyStore:
    return VenueAccuracyStore()


def meta_consensus(markets, accuracy_fn=None) -> dict:
    """Accuracy-weighted best-estimate probability + credible interval.

    ``accuracy_fn(venue) -> {weight, ...}`` supplies each venue's weight (defaults
    to the store). Falls back to equal weighting when weights are all zero.
    """
    if not markets:
        return {"estimate": None, "venues": []}
    if accuracy_fn is None:
        store = get_venue_accuracy()
        accuracy_fn = store.accuracy

    entries = []
    for m in markets:
        acc = accuracy_fn(m.venue.value)
        entries.append({"venue": m.venue.value, "price": round(m.yes_price, 4), **acc})

    total_w = sum(e["weight"] for e in entries)
    if total_w <= 0:  # degenerate -> equal weight
        for e in entries:
            e["weight"] = 1.0
        total_w = float(len(entries))
    estimate = sum(e["price"] * e["weight"] for e in entries) / total_w
    # Weighted dispersion -> a 1-sigma credible interval, clamped to [0,1].
    var = sum(e["weight"] * (e["price"] - estimate) ** 2 for e in entries) / total_w
    std = var ** 0.5
    return {
        "estimate": round(estimate, 4),
        "credible_interval": [round(max(0.0, estimate - std), 4),
                              round(min(1.0, estimate + std), 4)],
        "dispersion": round(std, 4),
        "venues": entries,
        "method": "calibration-weighted (Brier) meta-consensus",
    }
