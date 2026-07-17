"""B8: Manifold adapter — normalization + synthesized CPMM book."""

from __future__ import annotations

import httpx

from core.adapters import base, manifold
from core.models import Venue


MANIFOLD_MARKETS = [
    {
        "id": "abc123",
        "question": "Will BTC be above $100k at end of 2026?",
        "outcomeType": "BINARY",
        "probability": 0.64,
        "totalLiquidity": 4000,
        "volume": 25000,
        "closeTime": 1798675200000,  # ms epoch
        "isResolved": False,
    },
    {  # resolved -> skipped
        "id": "done", "question": "old", "outcomeType": "BINARY",
        "probability": 1.0, "isResolved": True,
    },
    {  # non-binary -> skipped
        "id": "multi", "question": "who wins?", "outcomeType": "MULTIPLE_CHOICE",
    },
]

MANIFOLD_MARKET = {"id": "abc123", "probability": 0.64, "totalLiquidity": 4000}


def _mock(routes):
    def handler(req):
        url = str(req.url)
        for key, payload in routes.items():
            if key in url:
                return httpx.Response(200, json=payload)
        return httpx.Response(404, json={})

    def make_client(base_url, timeout=None):
        return httpx.Client(base_url=base_url, transport=httpx.MockTransport(handler))

    return make_client


def test_manifold_market_normalization(monkeypatch):
    monkeypatch.setattr(base, "make_client", _mock({"/v0/markets": MANIFOLD_MARKETS}))
    base._CLIENTS.clear()
    markets = manifold.ManifoldAdapter().fetch_markets()
    assert len(markets) == 1  # resolved + non-binary skipped
    m = markets[0]
    assert m.venue == Venue.MANIFOLD
    assert m.market_id == "abc123"
    assert m.yes_price == 0.64 and m.no_price == 0.36
    assert m.volume_usd == 25000
    assert m.close_time is not None


def test_manifold_synthesized_book(monkeypatch):
    monkeypatch.setattr(base, "make_client", _mock({"/v0/market/abc123": MANIFOLD_MARKET}))
    base._CLIENTS.clear()
    book = manifold.ManifoldAdapter().fetch_orderbook("abc123")
    assert book is not None and book.venue == Venue.MANIFOLD
    # Best ask above prob, best bid below; both carry synthesized depth.
    assert book.yes_asks[0].price > 0.64 > book.yes_bids[0].price
    assert book.yes_asks[0].size_usd > 0


def test_manifold_registered_in_venue_enum():
    assert Venue.MANIFOLD.value == "manifold"
