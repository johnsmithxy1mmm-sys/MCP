"""Real-time price ingestion over venue WebSockets (B1).

OFF by default. When ``STREAMING`` is set, a background thread subscribes to each
venue's WSS price feed and writes every tick into the :class:`HistoryStore`, so
the free-tier delayed price and ``get_market_history`` reflect live movement
without the tool path polling the venue on every call.

Design for discipline:
* **Flag-gated + graceful degradation** — no ``STREAMING`` env, no ``websockets``
  package, or a dropped connection all leave the server running on its existing
  TTL-cached fetch path. A stream error is logged and retried with backoff; it
  never crashes a tool.
* **Testable offline** — the wire-message normalization (:func:`normalize_tick`)
  is pure and unit-tested with synthetic payloads; the socket loop is the only
  part that needs a network and it's isolated behind the flag.

  STREAMING            unset (default) | on  — enable the ingestor
  POLYMARKET_WSS_URL   wss://ws-subscriptions-clob.polymarket.com/ws/market
  KALSHI_WSS_URL       wss://api.elections.kalshi.com/trade-api/ws/v2
  STREAM_BACKOFF_MAX   max reconnect backoff seconds (default 30)
"""

from __future__ import annotations

import json
import logging
import os
import threading
from typing import Callable

from .models import Venue
from .storage import HistoryStore

log = logging.getLogger("predmarket.streaming")


def streaming_enabled() -> bool:
    return os.getenv("STREAMING", "").lower() in ("1", "on", "true", "yes")


def normalize_tick(venue: str, raw: dict) -> tuple[str, float] | None:
    """Map a venue WSS message to ``(market_id, yes_price)`` or None to skip.

    Pure and side-effect free so it can be unit-tested with recorded payloads.
    Unknown/non-price messages (heartbeats, subscription acks) return None.
    """
    if venue == Venue.POLYMARKET.value:
        # CLOB `price_change` / `book` messages carry an asset id + a price.
        if raw.get("event_type") not in ("price_change", "last_trade_price", "book"):
            return None
        market_id = raw.get("market") or raw.get("asset_id")
        price = raw.get("price") or raw.get("mid")
        if market_id is None or price is None:
            return None
        try:
            return str(market_id), _clamp(float(price))
        except (TypeError, ValueError):
            return None
    if venue == Venue.KALSHI.value:
        # Kalshi `ticker` messages carry `yes_bid`/`yes_ask` in cents.
        if raw.get("type") != "ticker":
            return None
        msg = raw.get("msg") or raw
        ticker = msg.get("market_ticker") or msg.get("ticker")
        bid, ask = msg.get("yes_bid"), msg.get("yes_ask")
        if ticker is None or bid is None or ask is None:
            return None
        try:
            mid = (float(bid) + float(ask)) / 200.0  # two cents -> prob midpoint
            return str(ticker), _clamp(mid)
        except (TypeError, ValueError):
            return None
    return None


def _clamp(p: float) -> float:
    return round(min(1.0, max(0.0, p)), 4)


class StreamIngestor:
    """Applies normalized ticks to the history store (and an optional sink).

    ``on_tick`` is the single ingestion point; the socket loop and tests both
    call it. ``sink`` lets the live engine invalidate its TTL cache on updates.
    """

    def __init__(
        self,
        history: HistoryStore | None = None,
        sink: Callable[[str, str, float], None] | None = None,
    ):
        self._history = history or HistoryStore()
        self._sink = sink
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def on_tick(self, venue: str, raw: dict) -> bool:
        """Normalize + persist one wire message. Returns True if it was a price."""
        parsed = normalize_tick(venue, raw)
        if parsed is None:
            return False
        market_id, yes_price = parsed
        try:
            self._history.record(venue, market_id, yes_price)
            if self._sink is not None:
                self._sink(venue, market_id, yes_price)
        except Exception:  # ingestion must never crash the stream loop
            log.exception("stream ingest failed for %s/%s", venue, market_id)
            return False
        return True

    # -- background loop (flag-gated, network) ----------------------------
    def start(self) -> "StreamIngestor | None":
        if not streaming_enabled():
            return None
        if self._thread and self._thread.is_alive():
            return self
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="stream-ingestor", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:  # pragma: no cover - needs a live socket
        try:
            import asyncio

            asyncio.run(self._consume_all())
        except Exception:
            log.exception("stream ingestor stopped")

    async def _consume_all(self) -> None:  # pragma: no cover - needs a live socket
        try:
            import websockets  # optional dependency
        except ImportError:
            log.warning("STREAMING set but `websockets` not installed; ingestor idle")
            return
        import asyncio

        feeds = {
            Venue.POLYMARKET.value: os.getenv(
                "POLYMARKET_WSS_URL",
                "wss://ws-subscriptions-clob.polymarket.com/ws/market",
            ),
            Venue.KALSHI.value: os.getenv(
                "KALSHI_WSS_URL", "wss://api.elections.kalshi.com/trade-api/ws/v2"
            ),
        }
        await asyncio.gather(
            *(self._consume_one(websockets, v, url) for v, url in feeds.items())
        )

    async def _consume_one(self, websockets, venue, url):  # pragma: no cover - socket
        import asyncio

        backoff = 1.0
        backoff_max = float(os.getenv("STREAM_BACKOFF_MAX", "30"))
        while not self._stop.is_set():
            try:
                async with websockets.connect(url) as ws:
                    backoff = 1.0  # reset on a clean connect
                    async for raw in ws:
                        if self._stop.is_set():
                            break
                        try:
                            self.on_tick(venue, json.loads(raw))
                        except (ValueError, TypeError):
                            continue
            except Exception:
                log.warning("%s stream reconnecting in %.0fs", venue, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff_max, backoff * 2)


_INGESTOR: StreamIngestor | None = None
_LOCK = threading.Lock()


def start_streaming(sink: Callable[[str, str, float], None] | None = None) -> StreamIngestor | None:
    """Start the ingestor if ``STREAMING`` is set. Idempotent; else returns None."""
    global _INGESTOR
    if not streaming_enabled():
        return None
    with _LOCK:
        if _INGESTOR is None:
            _INGESTOR = StreamIngestor(sink=sink)
            _INGESTOR.start()
    return _INGESTOR


def streaming_running() -> bool:
    return _INGESTOR is not None
