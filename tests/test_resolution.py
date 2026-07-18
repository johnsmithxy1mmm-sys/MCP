"""F1: autonomous outcome resolver + venue settlement parsing."""

from __future__ import annotations

import httpx
import pytest

from core import resolution
from core.resolution import ResolutionEngine, collect_outcomes
from core.models import Leg, Opportunity, OpportunityKind, Side, Venue
from core.reconciliation import ReconciliationStore


def _opp(mid="m1", venue=Venue.KALSHI, side=Side.YES):
    return Opportunity(kind=OpportunityKind.BUNDLE, title="t", category="crypto",
                       realizable_edge=0.05, max_size_usd=100.0,
                       legs=[Leg(venue=venue, market_id=mid, side=side)])


# --- reconciliation now records venue + exposes pending legs ----------------
def test_pending_legs_include_venue(tmp_path):
    store = ReconciliationStore(db_url=f"sqlite:///{tmp_path / 'r.db'}")
    store.record(_opp("m1", Venue.KALSHI), {"m1": 0.6})
    store.record(_opp("0xabc", Venue.POLYMARKET), {"0xabc": 0.4})
    assert set(store.pending_legs()) == {("kalshi", "m1"), ("polymarket", "0xabc")}


# --- collect_outcomes -------------------------------------------------------
def test_collect_outcomes_keeps_only_settled():
    pending = [("kalshi", "a"), ("kalshi", "b"), ("polymarket", "c")]

    def resolver(venue, mid):
        return {"a": 1, "b": None, "c": 0}[mid]  # b still open

    assert collect_outcomes(pending, resolver) == {"a": 1, "c": 0}


def test_collect_outcomes_survives_resolver_errors():
    def resolver(venue, mid):
        if mid == "boom":
            raise RuntimeError("venue down")
        return 1

    out = collect_outcomes([("kalshi", "boom"), ("kalshi", "ok")], resolver)
    assert out == {"ok": 1}  # error skipped, batch not poisoned


# --- engine ends-to-end (fake resolver) -------------------------------------
def test_resolve_once_settles_and_updates_track_record(tmp_path):
    store = ReconciliationStore(db_url=f"sqlite:///{tmp_path / 'r.db'}")
    store.record(_opp("m1", Venue.KALSHI, Side.YES), {"m1": 0.6})
    assert store.metrics()["resolved_count"] == 0

    engine = ResolutionEngine(store=store, resolver=lambda v, m: 1)  # market settled YES
    n = engine.resolve_once()
    assert n == 1
    metrics = store.metrics()
    assert metrics["resolved_count"] == 1
    assert metrics["pending_count"] == 0
    # A YES leg entered at 0.6 that resolves YES realized (1-0.6)/0.6 > 0.
    assert metrics["mean_realized_edge"] > 0
    assert engine.resolve_once() == 0  # nothing left pending


def test_resolve_once_noop_when_unsettled(tmp_path):
    store = ReconciliationStore(db_url=f"sqlite:///{tmp_path / 'r.db'}")
    store.record(_opp("m1"), {"m1": 0.6})
    engine = ResolutionEngine(store=store, resolver=lambda v, m: None)  # still open
    assert engine.resolve_once() == 0
    assert store.metrics()["resolved_count"] == 0


def test_engine_disabled_by_default(monkeypatch):
    monkeypatch.delenv("RESOLUTION_ENGINE", raising=False)
    assert resolution.start_resolution_engine() is None


# --- venue settlement parsing (mocked HTTP) ---------------------------------
def _adapter_with(routes):
    from core.adapters import base

    def make_client(base_url, timeout=None):
        def handler(req):
            url = str(req.url)
            for key, payload in routes.items():
                if key in url:
                    return httpx.Response(200, json=payload)
            return httpx.Response(404, json={})
        return httpx.Client(base_url=base_url, transport=httpx.MockTransport(handler))

    base._CLIENTS.clear()
    return make_client


def test_polymarket_resolution(monkeypatch):
    from core.adapters import base, polymarket

    monkeypatch.setattr(base, "make_client", _adapter_with({"/markets": [
        {"conditionId": "0xabc", "closed": True,
         "outcomes": "[\"Yes\",\"No\"]", "outcomePrices": "[\"1.0\",\"0.0\"]"}]}))
    assert polymarket.PolymarketAdapter().fetch_resolution("0xabc") == 1


def test_polymarket_unresolved_returns_none(monkeypatch):
    from core.adapters import base, polymarket

    monkeypatch.setattr(base, "make_client", _adapter_with({"/markets": [
        {"conditionId": "0xabc", "closed": False,
         "outcomes": "[\"Yes\",\"No\"]", "outcomePrices": "[\"0.7\",\"0.3\"]"}]}))
    assert polymarket.PolymarketAdapter().fetch_resolution("0xabc") is None


def test_kalshi_resolution(monkeypatch):
    from core.adapters import base, kalshi

    monkeypatch.setattr(base, "make_client",
                        _adapter_with({"/markets/KX-1": {"market": {"status": "settled", "result": "no"}}}))
    assert kalshi.KalshiAdapter().fetch_resolution("KX-1") == 0


def test_manifold_resolution(monkeypatch):
    from core.adapters import base, manifold

    monkeypatch.setattr(base, "make_client",
                        _adapter_with({"/v0/market/abc": {"isResolved": True, "resolution": "YES"}}))
    assert manifold.ManifoldAdapter().fetch_resolution("abc") == 1
