"""Shared test fixtures.

Points metering at a throwaway SQLite file (set BEFORE importing the app so the
module-level billing context picks it up) and clears it between tests.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path

import pytest

# Isolate metering to a temp DB before any predmarket import.
_TMP_DB = Path(tempfile.gettempdir()) / "predmarket_test_metering.db"
_TMP_RECON = Path(tempfile.gettempdir()) / "predmarket_test_recon.db"
_TMP_WATCH = Path(tempfile.gettempdir()) / "predmarket_test_watch.db"
_TMP_ANCHOR = Path(tempfile.gettempdir()) / "predmarket_test_anchor.db"
_TMP_PERSIST = Path(tempfile.gettempdir()) / "predmarket_test_persist.db"
_TMP_MATCHLEARN = Path(tempfile.gettempdir()) / "predmarket_test_matchlearn.db"
_TMP_AUDIT = Path(tempfile.gettempdir()) / "predmarket_test_audit.db"
_TMP_ACCURACY = Path(tempfile.gettempdir()) / "predmarket_test_accuracy.db"
_TMP_HOUSE = Path(tempfile.gettempdir()) / "predmarket_test_house.db"
_TMP_LEADERBOARD = Path(tempfile.gettempdir()) / "predmarket_test_leaderboard.db"
os.environ.setdefault("MATCHLEARN_DB_URL", f"sqlite:///{_TMP_MATCHLEARN}")
os.environ.setdefault("AUDIT_DB_URL", f"sqlite:///{_TMP_AUDIT}")
os.environ.setdefault("ACCURACY_DB_URL", f"sqlite:///{_TMP_ACCURACY}")
os.environ.setdefault("HOUSE_DB_URL", f"sqlite:///{_TMP_HOUSE}")
os.environ.setdefault("LEADERBOARD_DB_URL", f"sqlite:///{_TMP_LEADERBOARD}")
os.environ.setdefault("METERING_DB_URL", f"sqlite:///{_TMP_DB}")
os.environ.setdefault("RECON_DB_URL", f"sqlite:///{_TMP_RECON}")
os.environ.setdefault("WATCH_DB_URL", f"sqlite:///{_TMP_WATCH}")
os.environ.setdefault("ANCHOR_DB_URL", f"sqlite:///{_TMP_ANCHOR}")
os.environ.setdefault("PERSISTENCE_DB_URL", f"sqlite:///{_TMP_PERSIST}")
os.environ.setdefault("PAID_ENABLED", "false")

import pytest_asyncio  # noqa: E402

from fastmcp import Client  # noqa: E402

from predmarket_mcp.server import mcp  # noqa: E402
from predmarket_mcp.config import get_settings  # noqa: E402


def _db_path() -> str:
    url = get_settings().metering_db_url
    return url[len("sqlite:///"):] if url.startswith("sqlite:///") else url


@pytest.fixture(autouse=True)
def clean_metering():
    """Truncate usage + receipts before each test for deterministic counts."""
    path = _db_path()
    if Path(path).exists():
        with sqlite3.connect(path) as conn:
            for table in ("usage", "receipts"):
                try:
                    conn.execute(f"DELETE FROM {table}")
                except sqlite3.OperationalError:
                    pass
    if _TMP_RECON.exists():
        with sqlite3.connect(_TMP_RECON) as conn:
            try:
                conn.execute("DELETE FROM flagged")
            except sqlite3.OperationalError:
                pass
    if _TMP_WATCH.exists():
        with sqlite3.connect(_TMP_WATCH) as conn:
            for table in ("watches", "alerts"):
                try:
                    conn.execute(f"DELETE FROM {table}")
                except sqlite3.OperationalError:
                    pass
    # Embedding vector cache must not leak between tests (different fake embedders).
    from core import embeddings
    embeddings.clear_cache()
    # Pooled HTTP clients must not leak between tests: each test monkeypatches
    # base.make_client with its own MockTransport, and a cached client from a
    # prior test would silently ignore that patch.
    from core.adapters import base
    for _c in base._CLIENTS.values():
        try:
            _c.close()
        except Exception:
            pass
    base._CLIENTS.clear()
    # Circuit breakers are process-global; failures accumulated by one test's
    # deliberately-failing transport must not open the breaker for the next.
    from core import circuit
    circuit._BREAKERS.clear()
    # Calibrator is cached by resolved-sample count; reset so a test that seeds
    # the reconciliation store trains a fresh calibrator.
    from core import calibration
    calibration.clear_cache()
    # On-chain anchor store: clear rows + the singleton so anchor tests isolate.
    if _TMP_ANCHOR.exists():
        with sqlite3.connect(_TMP_ANCHOR) as conn:
            try:
                conn.execute("DELETE FROM anchors")
            except sqlite3.OperationalError:
                pass
    from core import anchor
    anchor.get_anchor_store.cache_clear()
    # Watch store singleton: reset so a test that swaps in a Redis client (or the
    # SQLite default) doesn't leak the cached store into the next test.
    from core import watches
    watches.get_watches.cache_clear()
    # Survival/persistence store: clear rows so lifespan stats don't leak.
    if _TMP_PERSIST.exists():
        with sqlite3.connect(_TMP_PERSIST) as conn:
            for table in ("sightings", "lifespans"):
                try:
                    conn.execute(f"DELETE FROM {table}")
                except sqlite3.OperationalError:
                    pass
    # Matcher-learning store: clear observations between tests.
    if _TMP_MATCHLEARN.exists():
        with sqlite3.connect(_TMP_MATCHLEARN) as conn:
            try:
                conn.execute("DELETE FROM match_obs")
            except sqlite3.OperationalError:
                pass
    from core import matchlearn
    matchlearn.get_matchlearn.cache_clear()
    # Audit-bundle store: clear rows + singleton between tests.
    if _TMP_AUDIT.exists():
        with sqlite3.connect(_TMP_AUDIT) as conn:
            try:
                conn.execute("DELETE FROM audit_bundles")
            except sqlite3.OperationalError:
                pass
    from core import auditbundle
    auditbundle.get_store.cache_clear()
    # Venue-accuracy store (meta-consensus): clear samples + singleton.
    if _TMP_ACCURACY.exists():
        with sqlite3.connect(_TMP_ACCURACY) as conn:
            try:
                conn.execute("DELETE FROM venue_samples")
            except sqlite3.OperationalError:
                pass
    from core import metaconsensus
    metaconsensus.get_venue_accuracy.cache_clear()
    # House-forecast store (K1): clear rows + singleton between tests.
    if _TMP_HOUSE.exists():
        with sqlite3.connect(_TMP_HOUSE) as conn:
            try:
                conn.execute("DELETE FROM house_forecasts")
            except sqlite3.OperationalError:
                pass
    from core import houseforecast
    houseforecast.get_house_forecasts.cache_clear()
    # Agent leaderboard store (K4): clear paper trades + singleton between tests.
    if _TMP_LEADERBOARD.exists():
        with sqlite3.connect(_TMP_LEADERBOARD) as conn:
            try:
                conn.execute("DELETE FROM paper_trades")
            except sqlite3.OperationalError:
                pass
    from core import leaderboard
    leaderboard.get_leaderboard.cache_clear()
    # Reality nowcast TTL cache (K7): a stubbed provider's value must not leak.
    from core import reality
    reality.clear_cache()
    yield


@pytest_asyncio.fixture
async def client():
    async with Client(mcp) as c:
        yield c
