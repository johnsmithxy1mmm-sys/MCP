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


class AdapterClientError(AdapterError):
    """A 4xx from the venue — the *request* was wrong (bad market id, auth),
    not the venue. Never counted by the circuit breaker."""


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


def normalize_rows(rows, normalizer, query: str | None = None) -> list["Market"]:
    """Normalize venue rows into Markets, skipping anything unusable.

    The single defensive boundary for venue payloads. A venue response is
    UNTRUSTED input: the shape is documented, not guaranteed. Previously a
    ``None`` inside the array, a wrong top-level type, or a price outside [0,1]
    either crashed past the AdapterError boundary (500 on a paid call) or
    produced a poisoned Market (audit INV-005 / INV-008). Here one bad row costs
    exactly that row.
    """
    if not isinstance(rows, list):
        return []
    out: list[Market] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            market = normalizer(row)
        except Exception:  # noqa: BLE001 - venue payloads are arbitrary
            continue      # malformed row / failed model validation -> skip it
        if market is None or not _matches_query(query, market):
            continue
        out.append(market)
    return out


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
    keep-alive client per host, and a per-host circuit breaker so a dead venue
    fast-fails instead of costing every call a full timeout.
    """
    from ..circuit import CircuitOpen, get_breaker

    breaker = get_breaker(venue or base_url)
    try:
        return breaker.call(
            _do_fetch, base_url, path, params, headers, venue,
            ignore=(AdapterClientError,),  # 4xx = bad request, not a down venue
        )
    except CircuitOpen as exc:
        raise AdapterError(f"{venue} circuit open: {exc}") from exc


def _do_fetch(base_url: str, path: str, params, headers, venue: str):
    try:
        resp = get_client(base_url).get(path, params=params, headers=headers)
    except httpx.HTTPError as exc:  # connect/proxy/timeout/etc.
        raise AdapterError(f"{venue} {path} request failed: {type(exc).__name__}") from exc
    if resp.status_code != 200:
        detail = f"{venue} {path} HTTP {resp.status_code}: {resp.text[:120]}"
        # 4xx (except 429, which signals venue pushback) is a caller error and
        # must not trip the venue circuit breaker.
        if 400 <= resp.status_code < 500 and resp.status_code != 429:
            raise AdapterClientError(detail)
        raise AdapterError(detail)
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

    def fetch_resolution(self, market_id: str) -> int | None:
        """Return 1/0 if the market has SETTLED yes/no, else None (unresolved or
        unknown). Used by the autonomous resolver to close the track record.
        Default: unknown — adapters that can determine settlement override it."""
        return None
