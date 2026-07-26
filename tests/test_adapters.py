"""Live-adapter normalization tests, driven by mocked HTTP.

The venue APIs aren't reachable from CI/sandboxes, so we feed recorded sample
payloads through httpx.MockTransport and assert the adapters normalize them into
canonical models correctly. No network involved.
"""

from __future__ import annotations


import httpx
import pytest

from core.adapters import base, kalshi, polymarket
from core.models import Venue


# --- sample payloads (trimmed, realistic shapes) ----------------------------
GAMMA_MARKETS = [
    {
        "id": "1234",
        "conditionId": "0xcond-btc",
        "question": "Will Bitcoin close above $100k at end of 2026?",
        "category": "Crypto",
        "outcomes": "[\"Yes\", \"No\"]",
        "outcomePrices": "[\"0.71\", \"0.29\"]",
        "clobTokenIds": "[\"tokenYES\", \"tokenNO\"]",
        "volume": "2040000",
        "endDate": "2026-12-31T00:00:00Z",
        "closed": False,
    },
    {  # malformed / non-binary -> skipped
        "id": "9999",
        "conditionId": "0xbad",
        "question": "Weird market",
        "outcomePrices": "[\"1.0\"]",
    },
]

CLOB_BOOK = {
    "asks": [{"price": "0.72", "size": "1500"}, {"price": "0.715", "size": "900"}],
    "bids": [{"price": "0.70", "size": "1200"}, {"price": "0.69", "size": "800"}],
}

KALSHI_MARKETS = {
    "markets": [
        {
            "ticker": "KXBTC-26",
            "title": "Bitcoin above $100,000 on Dec 31 2026",
            "category": "Crypto",
            "yes_bid": 64,
            "yes_ask": 68,
            "volume": 560000,
            "status": "open",
        }
    ]
}

KALSHI_BOOK = {
    "orderbook": {
        "yes": [[64, 500], [63, 700]],   # YES bids (cents, contracts)
        "no": [[33, 400], [32, 600]],    # NO bids -> YES asks = 1 - no
    }
}


def _mock_client_factory(routes):
    """make_client replacement routing by a substring of the FULL url.

    Routes are matched in insertion order (so put the more specific key, e.g.
    '/orderbook', before a broader one like '.../markets').
    """

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        for key, payload in routes.items():
            if key in url:
                return httpx.Response(200, json=payload)
        return httpx.Response(404, json={"error": "no route"})

    def make_client(base_url: str, timeout=None):
        return httpx.Client(base_url=base_url, transport=httpx.MockTransport(handler))

    return make_client


# --- Polymarket -------------------------------------------------------------
def test_polymarket_market_normalization(monkeypatch):
    monkeypatch.setattr(base, "make_client",
                        _mock_client_factory({"/markets": GAMMA_MARKETS}))
    adapter = polymarket.PolymarketAdapter()
    markets = adapter.fetch_markets()
    assert len(markets) == 1  # malformed row skipped
    m = markets[0]
    assert m.venue == Venue.POLYMARKET
    assert m.market_id == "0xcond-btc"
    assert m.yes_price == 0.71 and m.no_price == 0.29
    assert m.category == "crypto"
    assert m.volume_usd == 2040000
    # token map populated for the orderbook lookup
    assert adapter._token_by_market["0xcond-btc"] == "tokenYES"


def test_polymarket_orderbook_normalization(monkeypatch):
    # /book for depth; gamma /markets so an unknown conditionId resolves to its
    # YES clob token (the adapter must never send a conditionId as a token_id).
    monkeypatch.setattr(base, "make_client", _mock_client_factory({
        "/book": CLOB_BOOK,
        "gamma-api.polymarket.com/markets": GAMMA_MARKETS,
    }))
    adapter = polymarket.PolymarketAdapter()
    book = adapter.fetch_orderbook("0xcond-btc")  # token map empty -> Gamma fallback
    assert book.venue == Venue.POLYMARKET
    assert adapter._token_by_market["0xcond-btc"] == "tokenYES"
    # asks sorted ascending, bids descending
    assert book.yes_asks[0].price == 0.715
    assert book.yes_bids[0].price == 0.70


def test_polymarket_orderbook_unknown_market_returns_none(monkeypatch):
    # Unresolvable market (no token, Gamma returns nothing) -> None, never a
    # bogus book fetched with the conditionId as the token_id.
    monkeypatch.setattr(base, "make_client",
                        _mock_client_factory({"gamma-api.polymarket.com/markets": []}))
    adapter = polymarket.PolymarketAdapter()
    assert adapter.fetch_orderbook("0xunknown") is None


def test_polymarket_query_filter(monkeypatch):
    monkeypatch.setattr(base, "make_client",
                        _mock_client_factory({"/markets": GAMMA_MARKETS}))
    adapter = polymarket.PolymarketAdapter()
    assert adapter.fetch_markets(query="bitcoin")   # matches
    assert adapter.fetch_markets(query="election") == []  # filtered out


# --- Kalshi -----------------------------------------------------------------
def test_kalshi_market_normalization(monkeypatch):
    monkeypatch.setattr(base, "make_client",
                        _mock_client_factory({"/markets": KALSHI_MARKETS}))
    adapter = kalshi.KalshiAdapter()
    markets = adapter.fetch_markets()
    assert len(markets) == 1
    m = markets[0]
    assert m.venue == Venue.KALSHI
    assert m.market_id == "KXBTC-26"
    # midpoint of 64c/68c = 66c -> 0.66
    assert m.yes_price == 0.66
    assert m.no_price == 0.34
    assert m.category == "crypto"


def test_kalshi_orderbook_normalization(monkeypatch):
    monkeypatch.setattr(base, "make_client",
                        _mock_client_factory({"/orderbook": KALSHI_BOOK}))
    adapter = kalshi.KalshiAdapter()
    book = adapter.fetch_orderbook("KXBTC-26")
    assert book.yes_bids[0].price == 0.64          # best YES bid
    assert book.yes_asks[0].price == round(1 - 0.33, 4)  # best YES ask from NO bid


def test_adapter_error_on_bad_status(monkeypatch):
    def make_client(base_url, timeout=None):
        return httpx.Client(base_url=base_url,
                            transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    monkeypatch.setattr(base, "make_client", make_client)
    with pytest.raises(base.AdapterError):
        polymarket.PolymarketAdapter().fetch_markets()


# --- live engine over adapters ---------------------------------------------
def test_live_engine_scans_over_adapters(monkeypatch):
    """LiveRepo + algorithms produce opportunities from adapter data."""
    # One factory, host-qualified routes (order matters: /orderbook before markets).
    monkeypatch.setattr(base, "make_client", _mock_client_factory({
        "/orderbook": KALSHI_BOOK,
        "clob.polymarket.com/book": CLOB_BOOK,
        "gamma-api.polymarket.com/markets": GAMMA_MARKETS,
        "kalshi.com/trade-api/v2/markets": KALSHI_MARKETS,
    }))
    # Import after patching so the LiveRepo uses patched adapters.
    from core import live
    repo = live.LiveRepo()
    monkeypatch.setattr(live, "repo", repo)

    markets = repo._all_markets()
    venues = {m.venue for m in markets}
    assert venues == {Venue.POLYMARKET, Venue.KALSHI}

    # Both venues quote the same BTC event -> a cross-venue match should form.
    pair = live.match_event("bitcoin above 100k 2026")
    assert pair is not None
    assert {pair.a.venue, pair.b.venue} == {Venue.POLYMARKET, Venue.KALSHI}

    ops = live.scan_opportunities(0.0)
    assert any(o.kind.value == "cross_venue" for o in ops)


# --- INV-005 / INV-008: граница с площадкой (регрессии аудита) ---------------
def _poly_with(handler):
    """PolymarketAdapter поверх заданного ответа площадки."""
    import httpx
    from core.adapters import base
    from core import circuit

    base._CLIENTS.clear()
    circuit._BREAKERS.clear()
    orig = base.make_client
    base.make_client = lambda url, timeout=None: httpx.Client(
        base_url=url, transport=httpx.MockTransport(handler))
    try:
        from core.adapters.polymarket import PolymarketAdapter
        return PolymarketAdapter().fetch_markets()
    finally:
        base.make_client = orig
        base._CLIENTS.clear()


def _row(yes="0.5", no="0.5"):
    return {"conditionId": "c1", "question": "Will X?",
            "outcomePrices": f'["{yes}","{no}"]', "outcomes": '["Yes","No"]'}


def test_out_of_range_price_is_rejected_not_ingested():
    """INV-005: цена 999 от площадки не должна порождать Market.

    Раньше модель лишь ДОКУМЕНТИРОВАЛА диапазон [0,1] и принимала что угодно —
    один такой рынок отравлял edge, вероятности, house-прогноз, калибровку и
    попадал в историю. Теперь он отбраковывается на границе.
    """
    import httpx

    ms = _poly_with(lambda r: httpx.Response(200, json=[_row("999", "-5")]))
    assert ms == []
    # ...а исправный рынок в том же ответе по-прежнему проходит.
    ms = _poly_with(lambda r: httpx.Response(200, json=[_row("999", "-5"), _row()]))
    assert len(ms) == 1 and 0.0 <= ms[0].yes_price <= 1.0


def test_model_enforces_price_range():
    """Инвариант держится на уровне модели, а не только адаптера."""
    import pytest as _pytest
    from pydantic import ValidationError
    from core.models import Market, Venue

    with _pytest.raises(ValidationError):
        Market(venue=Venue.KALSHI, market_id="m", title="t", yes_price=999.0, no_price=0.0)
    with _pytest.raises(ValidationError):
        Market(venue=Venue.KALSHI, market_id="m", title="t", yes_price=0.5, no_price=-0.1)


def test_malformed_rows_skip_instead_of_crashing():
    """INV-008: None/не-словарь в массиве ронял адаптер мимо границы
    AdapterError, то есть 500 на платном вызове."""
    import httpx

    ms = _poly_with(lambda r: httpx.Response(200, json=[None, _row(), "строка", 42]))
    assert len(ms) == 1                      # уцелел ровно исправный рынок

    ms = _poly_with(lambda r: httpx.Response(200, json=[[[[]]], {"nested": {"deep": 1}}]))
    assert ms == []                          # без исключения


def test_wrong_top_level_shape_yields_no_markets():
    import httpx

    assert _poly_with(lambda r: httpx.Response(200, json={"unexpected": "shape"})) == []
