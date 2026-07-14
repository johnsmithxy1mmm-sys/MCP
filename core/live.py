"""Live engine: real venue adapters + shared algorithms.

Exposes the SAME surface as ``core.mock`` (``repo`` + ``match_event`` +
``scan_opportunities`` + ``realizable_edge``) so ``deps.py`` can swap between
them with a flag and nothing else in the MCP layer changes.

Markets/orderbooks are cached briefly (TTL) to avoid hammering venue APIs on
every tool call. History is not available live yet (needs a time-series store).
"""

from __future__ import annotations

import os
import time
from datetime import datetime

from . import algorithms
from .adapters import AdapterError, KalshiAdapter, PolymarketAdapter
from .storage import HistoryStore
from .models import (
    ExecutionEstimate,
    Leg,
    Market,
    MatchedPair,
    OrderbookSnapshot,
    Opportunity,
    PricePoint,
    Venue,
)

_MARKET_TTL = float(os.getenv("LIVE_MARKET_TTL", "30"))
_BOOK_TTL = float(os.getenv("LIVE_BOOK_TTL", "5"))


class _TTLCache:
    def __init__(self, ttl: float):
        self.ttl = ttl
        self._store: dict = {}

    def get(self, key):
        hit = self._store.get(key)
        if hit and (time.monotonic() - hit[0]) < self.ttl:
            return hit[1]
        return None

    def put(self, key, value):
        self._store[key] = (time.monotonic(), value)


class LiveRepo:
    """Storage-repo interface backed by live adapters (with TTL caching)."""

    def __init__(self):
        self._adapters = {
            Venue.POLYMARKET.value: PolymarketAdapter(),
            Venue.KALSHI.value: KalshiAdapter(),
        }
        self._markets = _TTLCache(_MARKET_TTL)
        self._books = _TTLCache(_BOOK_TTL)
        self._history = HistoryStore()

    # -- internal ---------------------------------------------------------
    def _all_markets(self, query: str | None = None) -> list[Market]:
        cached = self._markets.get(query or "*")
        if cached is not None:
            return cached
        out: list[Market] = []
        for adapter in self._adapters.values():
            try:
                out.extend(adapter.fetch_markets(query=query))
            except AdapterError:
                continue  # one venue down shouldn't kill the whole response
        # Ingest a history point for every observed market (fresh fetch only).
        for m in out:
            self._history.record_market(m)
        self._markets.put(query or "*", out)
        return out

    # -- repo surface (mirrors core.mock._Repo) ---------------------------
    def search_markets(
        self, query: str, category: str | None = None, venue: str | None = None
    ) -> list[Market]:
        out = self._all_markets(query)
        if category:
            out = [m for m in out if (m.category or "") == category]
        if venue:
            out = [m for m in out if m.venue.value == venue]
        return out

    def get_market(self, venue: str, market_id: str) -> Market | None:
        for m in self._all_markets():
            if m.venue.value == venue and m.market_id == market_id:
                return m
        return None

    def get_orderbook(self, venue: str, market_id: str) -> OrderbookSnapshot | None:
        key = (venue, market_id)
        cached = self._books.get(key)
        if cached is not None:
            return cached
        adapter = self._adapters.get(venue)
        if not adapter:
            return None
        try:
            book = adapter.fetch_orderbook(market_id)
        except AdapterError:
            return None
        self._books.put(key, book)
        return book

    def get_history(
        self, venue: str, market_id: str, frm: datetime, to: datetime
    ) -> list[PricePoint]:
        # Reads points ingested by _all_markets() into the HistoryStore.
        # Backfill depth grows with server uptime (or a Timescale DSN).
        return self._history.query(venue, market_id, frm, to)

    def list_venues(self) -> list[dict]:
        counts: dict[str, int] = {}
        cats: dict[str, set] = {}
        for m in self._all_markets():
            counts[m.venue.value] = counts.get(m.venue.value, 0) + 1
            if m.category:
                cats.setdefault(m.venue.value, set()).add(m.category)
        return [
            {
                "venue": v,
                "status": "live",
                "markets_covered": counts.get(v, 0),
                "categories": sorted(cats.get(v, set())),
            }
            for v in self._adapters
        ]


repo = LiveRepo()


# --- engine surface (mirrors core.mock) -------------------------------------
def match_event(event: str) -> MatchedPair | None:
    return algorithms.best_match(repo._all_markets(event), event)


def scan_opportunities(
    min_edge: float, kind: str | None = None, category: str | None = None
) -> list[Opportunity]:
    markets = repo._all_markets()
    out: list[Opportunity] = []
    if kind in (None, "cross_venue"):
        out += algorithms.scan_cross_venue(
            algorithms.match_markets(markets), min_edge, category
        )
    if kind in (None, "bundle"):
        out += algorithms.scan_bundle(markets, min_edge, category)
    out.sort(key=lambda o: o.realizable_edge, reverse=True)
    return out


def realizable_edge(legs: list[Leg], size_usd: float) -> ExecutionEstimate:
    return algorithms.estimate_realizable_edge(legs, size_usd, repo.get_orderbook)
