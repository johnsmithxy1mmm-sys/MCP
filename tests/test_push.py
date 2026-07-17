"""B2: alert push — webhook delivery + non-draining peek/resource."""

from __future__ import annotations

import pytest

from core import notifier
from core.models import Leg, Opportunity, OpportunityKind, Side, Venue
from core.watches import WatchStore


def _opp(edge=0.05):
    return Opportunity(
        kind=OpportunityKind.BUNDLE, title="btc bundle", category="crypto",
        realizable_edge=edge, max_size_usd=100.0,
        legs=[Leg(venue=Venue.KALSHI, market_id="m1", side=Side.YES)],
    )


def test_notify_noop_when_unconfigured(monkeypatch):
    monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)
    assert notifier.notify_alerts([{"x": 1}]) is False


def test_notify_posts_to_webhook(monkeypatch):
    posted = {}

    class _Resp:
        status_code = 204

    class _Client:
        def __init__(self, *a, **k): ...
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, url, json, headers):
            posted["url"] = url
            posted["json"] = json
            posted["auth"] = headers.get("Authorization")
            return _Resp()

    monkeypatch.setenv("ALERT_WEBHOOK_URL", "https://hook.example/alerts")
    monkeypatch.setenv("ALERT_WEBHOOK_SECRET", "s3cret")
    import httpx
    monkeypatch.setattr(httpx, "Client", _Client)

    assert notifier.notify_alerts([{"a": 1}]) is True
    assert posted["url"] == "https://hook.example/alerts"
    assert posted["json"] == {"alerts": [{"a": 1}]}
    assert posted["auth"] == "Bearer s3cret"


def test_notify_swallows_errors(monkeypatch):
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "https://hook.example/alerts")
    import httpx

    def _boom(*a, **k):
        raise httpx.HTTPError("down")

    monkeypatch.setattr(httpx, "Client", _boom)
    assert notifier.notify_alerts([{"a": 1}]) is False  # never raises


def test_fire_pushes_new_alerts(tmp_path, monkeypatch):
    pushed = []
    monkeypatch.setattr("core.notifier.notify_alerts", lambda alerts: pushed.append(alerts) or True)

    store = WatchStore(db_url=f"sqlite:///{tmp_path / 'w.db'}")
    store.create_watch("c1", 0.01)
    assert store.fire([_opp()]) == 1
    assert pushed and pushed[0][0]["client_id"] == "c1"
    # Deduped: a second fire of the same opp queues nothing and pushes nothing.
    pushed.clear()
    assert store.fire([_opp()]) == 0
    assert pushed == []


def test_peek_does_not_drain(tmp_path):
    store = WatchStore(db_url=f"sqlite:///{tmp_path / 'w.db'}")
    store.create_watch("c1", 0.01)
    store.fire([_opp()])
    assert len(store.peek("c1")) == 1
    assert len(store.peek("c1")) == 1     # peek is idempotent
    assert len(store.drain("c1")) == 1    # drain consumes
    assert store.peek("c1") == []         # now empty
