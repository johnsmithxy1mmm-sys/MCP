"""Adapter base: shared HTTP client and the venue-adapter contract."""

from __future__ import annotations

import atexit
import os
import threading
from abc import ABC, abstractmethod

import httpx

from ..models import Market, OrderbookSnapshot, Venue


class AdapterError(RuntimeError):
    """Raised when a venue API call fails or returns something unusable."""


# One pooled client per base_url — reused across calls so we keep connections
# warm (HTTP keep-alive) instead of paying a fresh TCP+TLS handshake per fetch.
_CLIENTS: dict[str, httpx.Client] = {}
_CLIENTS_LOCK = threading.Lock()


def _matches_query(query: str | None, market: "Market") -> bool:
    """Token-based keyword filter (any token hits), matching mock search UX."""
    if not query:
        return True
    hay = f"{market.title} {market.category or ''} {market.market_id}".lower()
    return any(tok in hay for tok in query.lower().split())


def make_client(base_url: str, timeout: float | None = None) -> httpx.Client:
    """HTTP client honoring env config.

    Respects the ambient proxy (``trust_env=True``) so it works behind the
    agent proxy / corporate egress. Timeout is overridable via HTTP_TIMEOUT.
    """
    t = timeout if timeout is not None else float(os.getenv("HTTP_TIMEOUT", "12"))
    return httpx.Client(
        base_url=base_url,
        timeout=t,
        follow_redirects=True,
        headers={"User-Agent": "predmarket-mcp/0.1 (+https://github.com)"},
    )


def get_client(base_url: str) -> httpx.Client:
    """Return the pooled client for ``base_url`` (created once, thread-safe)."""
    client = _CLIENTS.get(base_url)
    if client is not None:
        return client
    with _CLIENTS_LOCK:
        client = _CLIENTS.get(base_url)
        if client is None:
            client = make_client(base_url)
            _CLIENTS[base_url] = client
        return client


@atexit.register
def _close_clients() -> None:
    for client in _CLIENTS.values():
        try:
            client.close()
        except Exception:
            pass


def fetch_json(base_url: str, path: str, *, params=None, headers=None, venue: str = ""):
    """GET ``path`` and return parsed JSON, mapping ANY failure to AdapterError.

    This is the single network boundary: connection errors, timeouts, non-200
    statuses, and bad JSON all surface as ``AdapterError`` so callers (the live
    engine) can degrade gracefully instead of crashing a tool. Uses a pooled,
    keep-alive client per host instead of opening a new connection per call.
    """
    try:
        resp = get_client(base_url).get(path, params=params, headers=headers)
    except httpx.HTTPError as exc:  # connect/proxy/timeout/etc.
        raise AdapterError(f"{venue} {path} request failed: {type(exc).__name__}") from exc
    if resp.status_code != 200:
        raise AdapterError(f"{venue} {path} HTTP {resp.status_code}: {resp.text[:120]}")
    try:
        return resp.json()
    except ValueError as exc:
        raise AdapterError(f"{venue} {path} returned non-JSON") from exc


class VenueAdapter(ABC):
    """Contract every venue adapter implements.

    Adapters ONLY fetch + normalize. All intelligence (matching, signals, edge)
    lives in ``core.algorithms`` and is venue-agnostic.
    """

    venue: Venue

    @abstractmethod
    def fetch_markets(
        self, query: str | None = None, limit: int = 50
    ) -> list[Market]:
        """Return normalized markets, optionally keyword-filtered."""

    @abstractmethod
    def fetch_orderbook(self, market_id: str) -> OrderbookSnapshot | None:
        """Return a normalized top-of-book snapshot for one market."""
