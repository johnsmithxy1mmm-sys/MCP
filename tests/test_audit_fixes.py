"""Regression tests for the full-code audit fixes.

Each test pins one found-and-fixed defect so it can't regress:
4xx vs circuit breaker, zero-price ticks, embed-cache overflow, rate-limiter
bucket bound, alerts:// identity enforcement, history-prune first run.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest


# --- circuit breaker must ignore 4xx client errors --------------------------
def test_client_errors_do_not_trip_the_breaker(monkeypatch):
    from core import circuit
    from core.adapters import base

    monkeypatch.setenv("CIRCUIT_FAIL_MAX", "2")
    circuit._BREAKERS.pop("V404", None)
    base._CLIENTS.clear()

    def make_client(base_url, timeout=None):
        return httpx.Client(base_url=base_url,
                            transport=httpx.MockTransport(lambda r: httpx.Response(404)))

    monkeypatch.setattr(base, "make_client", make_client)
    # Many bad-market-id lookups (404) — an AdapterClientError each time, but the
    # breaker must stay closed: the venue is fine, the requests were wrong.
    for _ in range(6):
        with pytest.raises(base.AdapterClientError):
            base.fetch_json("https://x", "/bad", venue="V404")
    assert circuit.get_breaker("V404").state == "closed"


def test_429_still_counts_as_venue_pushback(monkeypatch):
    from core import circuit
    from core.adapters import base

    monkeypatch.setenv("CIRCUIT_FAIL_MAX", "2")
    circuit._BREAKERS.pop("V429", None)
    base._CLIENTS.clear()

    def make_client(base_url, timeout=None):
        return httpx.Client(base_url=base_url,
                            transport=httpx.MockTransport(lambda r: httpx.Response(429)))

    monkeypatch.setattr(base, "make_client", make_client)
    for _ in range(2):
        with pytest.raises(base.AdapterError):
            base.fetch_json("https://x", "/y", venue="V429")
    assert circuit.get_breaker("V429").state == "open"  # back off when throttled


# --- streaming: a price of zero is a real quote ------------------------------
def test_zero_price_tick_is_not_dropped():
    from core.streaming import normalize_tick

    tick = normalize_tick("polymarket", {
        "event_type": "price_change", "market": "0xdead", "price": 0,
    })
    assert tick == ("0xdead", 0.0)


# --- embeddings: one call larger than the cache bound ------------------------
def test_embed_texts_survives_cache_overflow(monkeypatch):
    from core import embeddings

    class FakeEmbedder:
        def embed(self, texts):
            return [[float(len(t))] for t in texts]

    monkeypatch.setattr(embeddings, "get_embedder", lambda: FakeEmbedder())
    monkeypatch.setattr(embeddings, "_CACHE_SIZE", 2)
    embeddings.clear_cache()
    texts = [f"title-{i}" for i in range(10)] + ["title-0"]  # dup at the end
    vectors = embeddings.embed_texts(texts)
    assert all(v is not None for v in vectors)          # no Nones despite eviction
    assert vectors[0] == vectors[-1]                    # duplicate maps consistently
    assert len(embeddings._CACHE) <= 2                  # bound still enforced
    embeddings.clear_cache()


# --- rate limiter: bucket map is bounded -------------------------------------
def test_rate_limiter_bucket_map_is_bounded(monkeypatch):
    from predmarket_mcp.ops import RateLimiter

    rl = RateLimiter(rpm=60, burst=1)
    monkeypatch.setattr(RateLimiter, "MAX_BUCKETS", 5)
    for i in range(50):
        rl.allow(f"client-{i}")
    assert len(rl._buckets) <= 5


# --- alerts:// is addressable only as "me" (audit INV-002) --------------------
@pytest.mark.asyncio
async def test_alerts_resource_is_scoped_to_the_caller(client):
    import json

    from core.watches import get_watches
    from core.models import Leg, Opportunity, OpportunityKind, Side, Venue

    # Queue an alert belonging to somebody else.
    store = get_watches()
    store.create_watch("someone-else", 0.01)
    store.fire([Opportunity(
        kind=OpportunityKind.BUNDLE, title="x", category=None,
        realizable_edge=0.5, max_size_usd=10.0,
        legs=[Leg(venue=Venue.KALSHI, market_id="m", side=Side.YES)],
    )])

    # There is no URI that names another principal: the old ownership "check"
    # compared two caller-controlled values and was vacuous (audit INV-002).
    with pytest.raises(Exception):
        await client.read_resource("alerts://someone-else")

    # Your own alerts resolve from a DERIVED identity and never include theirs.
    own = json.loads((await client.read_resource("alerts://me"))[0].text)
    assert "error" not in own
    assert own["count"] == 0                       # the foreign alert is not visible
    assert own["identity_proven"] is False         # in-process caller is ephemeral


# --- history prune runs on the first write -----------------------------------
def test_history_prune_runs_immediately(tmp_path, monkeypatch):
    monkeypatch.setenv("HISTORY_RETENTION_DAYS", "30")
    from core.storage import HistoryStore

    url = f"sqlite:///{tmp_path / 'h.db'}"
    old = datetime.now(timezone.utc) - timedelta(days=90)
    HistoryStore(db_url=url).record("kalshi", "m-old", 0.5, ts=old)  # ancient point
    # A fresh store (e.g. process restart) must prune on its FIRST write — even
    # on a freshly-booted machine where time.monotonic() is still tiny.
    HistoryStore(db_url=url).record("kalshi", "m-new", 0.6)
    pts = HistoryStore(db_url=url).query(
        "kalshi", "m-old", old - timedelta(days=1), datetime.now(timezone.utc)
    )
    assert pts == []  # ancient point pruned on first opportunity
