"""Autonomous outcome resolver — closes the trust flywheel (F1).

The track record (hit-rate, edge-slippage, Brier) and the calibration engine are
the server's self-growing moat, but they only grow when settled market outcomes
are fed back in. This engine does that automatically: it reads the (venue,
market_id) legs of every still-pending flagged opportunity, asks each venue
whether the market has SETTLED, and feeds the resolved outcomes into the
reconciliation store. With it running, the moat compounds 24/7 with no operator
in the loop.

OFF by default (``RESOLUTION_ENGINE`` unset) so offline tests and mock deploys
stay inert. A background thread polls on an interval; a single ``resolve_once``
pass is exposed for tests and manual/cron use. Every venue call degrades to
"unknown" on failure, so a flaky venue never stalls the loop.

  RESOLUTION_ENGINE           unset (default) | on
  RESOLUTION_INTERVAL         seconds between passes (default 3600)
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Callable

log = logging.getLogger("predmarket.resolution")

# A resolver maps (venue, market_id) -> 1/0 settled, or None if still open.
Resolver = Callable[[str, str], "int | None"]


def resolution_enabled() -> bool:
    return os.getenv("RESOLUTION_ENGINE", "").lower() in ("1", "on", "true", "yes")


def collect_outcomes(pending: list[tuple[str, str]], resolver: Resolver) -> dict[str, int]:
    """Query ``resolver`` for each pending (venue, market_id); keep the settled.

    Pure orchestration: a resolver error/None just means "not settled yet" and is
    skipped, so one bad venue can't poison the batch.
    """
    outcomes: dict[str, int] = {}
    for venue, market_id in pending:
        try:
            result = resolver(venue, market_id)
        except Exception:
            result = None
        if result in (0, 1):
            outcomes[market_id] = int(result)
    return outcomes


class ResolutionEngine:
    """Background loop: pending legs -> venue settlement -> reconciliation.resolve."""

    def __init__(self, store=None, resolver: Resolver | None = None, interval: float | None = None):
        self._store = store
        self._resolver = resolver
        self._interval = interval if interval is not None else float(
            os.getenv("RESOLUTION_INTERVAL", "3600"))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @staticmethod
    def enabled() -> bool:
        return resolution_enabled()

    def _get_store(self):
        if self._store is not None:
            return self._store
        from .reconciliation import get_reconciliation

        return get_reconciliation()

    def _get_resolver(self) -> Resolver:
        if self._resolver is not None:
            return self._resolver
        return _live_resolver

    def resolve_once(self) -> int:
        """One pass: check every pending market, resolve the settled ones.

        Returns the number of opportunities newly resolved.
        """
        store = self._get_store()
        pending = store.pending_legs()
        if not pending:
            return 0
        outcomes = collect_outcomes(pending, self._get_resolver())
        if not outcomes:
            return 0
        try:
            return store.resolve(outcomes)
        except Exception:
            log.exception("reconciliation.resolve failed")
            return 0

    # -- background loop --------------------------------------------------
    def start(self) -> "ResolutionEngine":
        if self._thread and self._thread.is_alive():
            return self
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="resolution-engine", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                n = self.resolve_once()
                if n:
                    log.info("resolution engine resolved %d opportunities", n)
            except Exception:
                log.exception("resolution pass failed")
            waited = 0.0
            while waited < self._interval and not self._stop.is_set():
                time.sleep(min(1.0, self._interval - waited))
                waited += 1.0


def _live_resolver(venue: str, market_id: str) -> int | None:
    """Ask the live engine's venue adapter whether a market has settled."""
    try:
        from .live import repo

        adapter = repo._adapters.get(venue)
        if adapter is None:
            return None
        return adapter.fetch_resolution(market_id)
    except Exception:
        return None


_ENGINE: ResolutionEngine | None = None
_LOCK = threading.Lock()


def start_resolution_engine(store=None, resolver: Resolver | None = None) -> ResolutionEngine | None:
    """Start the background resolver if ``RESOLUTION_ENGINE`` is set. Idempotent."""
    global _ENGINE
    if not resolution_enabled():
        return None
    with _LOCK:
        if _ENGINE is None:
            _ENGINE = ResolutionEngine(store=store, resolver=resolver).start()
    return _ENGINE


def resolution_engine_running() -> bool:
    return _ENGINE is not None
