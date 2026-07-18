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
os.environ.setdefault("METERING_DB_URL", f"sqlite:///{_TMP_DB}")
os.environ.setdefault("RECON_DB_URL", f"sqlite:///{_TMP_RECON}")
os.environ.setdefault("WATCH_DB_URL", f"sqlite:///{_TMP_WATCH}")
os.environ.setdefault("ANCHOR_DB_URL", f"sqlite:///{_TMP_ANCHOR}")
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
    yield


@pytest_asyncio.fixture
async def client():
    async with Client(mcp) as c:
        yield c
