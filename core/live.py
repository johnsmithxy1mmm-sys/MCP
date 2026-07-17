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
from collections import OrderedDict
from datetime import datetime

from . import algorithms
from .adapters import AdapterError, KalshiAdapter, ManifoldAdapter, PolymarketAdapter
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
_CACHE_MAX = int(os.getenv("LIVE_CACHE_MAX", "5000"))


class _TTLCache:
    """TTL cache with a bounded size (LRU eviction) so it can't grow forever."""

    def __init__(self, ttl: float, max_entries: int = _CACHE_MAX):
        self.ttl = ttl
        self.max_entries = max_entries
        self._store: "OrderedDict[object, tuple]" = OrderedDict()

    def get(self, key):
        hit = self._store.get(key)
        if hit and (time.monotonic() - hit[0]) < self.ttl:
            self._store.move_to_end(key)
            return hit[1]
        if hit:
            self._store.pop(key, None)  # expired
        return None

    def put(self, key, value):
        self._store[key] = (time.monotonic(), value)
        self._store.move_to_end(key)
        while len(self._store) > self.max_entries:
            self._store.popitem(last=False)


class LiveRepo:
    """Storage-repo interface backed by live adapters (with TTL caching)."""

    def __init__(self):
        self._adapters = {
            Venue.POLYMARKET.value: PolymarketAdapter(),
            Venue.KALSHI.value: KalshiAdapter(),
        }
        # Manifold is opt-in (play-money probabilities; useful as a 3rd consensus
        # anchor for fair value). Enable with MANIFOLD_ENABLED=on.
        if os.getenv("MANIFOLD_ENABLED", "").lower() in ("1", "on", "true", "yes"):
            self._adapters[Venue.MANIFOLD.value] = ManifoldAdapter()
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


_last_invalidate = 0.0


def _stream_sink(venue: str, market_id: str, yes_price: float) -> None:
    """Invalidate the cached market list on live ticks — throttled.

    Clearing on EVERY tick would defeat the TTL cache under a busy stream and
    hammer venue APIs on each tool call; at most one invalidation per half-TTL
    halves worst-case staleness without cache thrash. (History already gets
    every tick regardless — this only affects the cached market list.)
    """
    global _last_invalidate
    now = time.monotonic()
    if now - _last_invalidate >= _MARKET_TTL / 2:
        _last_invalidate = now
        repo._markets._store.clear()


# Real-time ingestion (STREAMING=on). Off by default; writes ticks into the same
# history store the delayed free tier and get_market_history read from.
from .streaming import start_streaming  # noqa: E402

start_streaming(sink=_stream_sink)


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
    if kind in (None, "dutch_book"):
        out += algorithms.scan_dutch_book(markets, min_edge, category)
    if kind in (None, "entailment"):
        from .entailment import scan_entailment

        cat_markets = [m for m in markets if not category or (m.category or "") == category]
        out += scan_entailment(cat_markets, min_edge)
    # Risk-adjust (latest close across all legs) + cap size by live book depth.
    return algorithms.finalize_opportunities(out, markets, repo.get_orderbook)


def realizable_edge(legs: list[Leg], size_usd: float) -> ExecutionEstimate:
    return algorithms.estimate_realizable_edge(legs, size_usd, repo.get_orderbook)
