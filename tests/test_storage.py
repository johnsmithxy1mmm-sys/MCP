"""History store + live-ingest tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from core.models import Market, Venue
from core.storage import HistoryStore


def _store(tmp_path) -> HistoryStore:
    return HistoryStore(f"sqlite:///{tmp_path / 'hist.db'}")


def test_record_and_query_range(tmp_path):
    store = _store(tmp_path)
    base = datetime(2026, 6, 1, tzinfo=timezone.utc)
    for i in range(5):
        store.record("polymarket", "pm-x", 0.5 + i * 0.01, spread=0.01,
                     ts=base + timedelta(hours=i))
    # Query a sub-window [t1, t3] -> 3 points, ascending.
    points = store.query("polymarket", "pm-x",
                         base + timedelta(hours=1), base + timedelta(hours=3))
    assert [p.yes_price for p in points] == [0.51, 0.52, 0.53]
    assert points[0].ts < points[-1].ts


def test_query_isolates_by_market(tmp_path):
    store = _store(tmp_path)
    now = datetime.now(timezone.utc)
    store.record("polymarket", "a", 0.4, ts=now)
    store.record("kalshi", "b", 0.6, ts=now)
    got = store.query("polymarket", "a", now - timedelta(minutes=1), now + timedelta(minutes=1))
    assert len(got) == 1 and got[0].yes_price == 0.4


def test_record_market_computes_spread(tmp_path):
    store = _store(tmp_path)
    now = datetime.now(timezone.utc)
    m = Market(venue=Venue.KALSHI, market_id="k1", title="t",
               yes_price=0.6, no_price=0.42)  # yes+no = 1.02 -> spread 0.02
    store.record_market(m, ts=now)
    pts = store.query("kalshi", "k1", now - timedelta(minutes=1), now + timedelta(minutes=1))
    assert pts[0].spread == 0.02


def test_live_ingest_then_history(tmp_path, monkeypatch):
    """LiveRepo records a point per fetched market; get_history reads it back."""
    monkeypatch.setenv("HISTORY_DB_URL", f"sqlite:///{tmp_path / 'live_hist.db'}")

    import httpx
    from core.adapters import base, kalshi, polymarket

    gamma = [{
        "id": "1", "conditionId": "0xc", "question": "Will BTC be above 100k in 2026?",
        "category": "Crypto", "outcomes": "[\"Yes\",\"No\"]",
        "outcomePrices": "[\"0.7\",\"0.3\"]", "clobTokenIds": "[\"tY\",\"tN\"]",
        "volume": "1000",
    }]

    def handler(request):
        url = str(request.url)
        if "gamma-api.polymarket.com/markets" in url:
            return httpx.Response(200, json=gamma)
        return httpx.Response(200, json={"markets": []})

    def make_client(base_url, timeout=None):
        return httpx.Client(base_url=base_url, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(base, "make_client", make_client)

    from core import live
    repo = live.LiveRepo()
    markets = repo._all_markets()          # triggers ingest
    assert markets and repo._history.count() >= 1

    now = datetime.now(timezone.utc)
    hist = repo.get_history("polymarket", "0xc", now - timedelta(minutes=1), now + timedelta(minutes=1))
    assert hist and hist[0].yes_price == 0.7
