"""Embedder backend + batched-cache tests (no network)."""

from __future__ import annotations

import httpx
import pytest

from core import embeddings


# --- VoyageEmbedder (mocked HTTP) -------------------------------------------
def _voyage_client(handler):
    return lambda: httpx.Client(base_url=embeddings.VOYAGE_URL, transport=httpx.MockTransport(handler))


def test_voyage_embed_parses_and_orders():
    def handler(request: httpx.Request) -> httpx.Response:
        body = httpx.Response(200)  # placeholder
        payload = {"data": [
            {"index": 1, "embedding": [0.0, 1.0]},   # returned out of order
            {"index": 0, "embedding": [1.0, 0.0]},
        ]}
        return httpx.Response(200, json=payload)

    emb = embeddings.VoyageEmbedder("voyage-3.5-lite", api_key="k", client_factory=_voyage_client(handler))
    vecs = emb.embed(["a", "b"])
    assert vecs == [[1.0, 0.0], [0.0, 1.0]]  # reordered by index to match input


def test_voyage_embed_maps_http_error():
    handler = lambda r: httpx.Response(401, json={"error": "bad key"})
    emb = embeddings.VoyageEmbedder("voyage-3.5-lite", api_key="k", client_factory=_voyage_client(handler))
    with pytest.raises(embeddings.EmbedderError):
        emb.embed(["a"])


def test_voyage_embed_maps_connection_error():
    def handler(r):
        raise httpx.ConnectError("blocked")

    emb = embeddings.VoyageEmbedder("voyage-3.5-lite", api_key="k", client_factory=_voyage_client(handler))
    with pytest.raises(embeddings.EmbedderError):
        emb.embed(["a"])


def test_get_embedder_none_without_voyage_key(monkeypatch):
    monkeypatch.setenv("EMBED_BACKEND", "voyage")
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    embeddings.get_embedder.cache_clear()
    assert embeddings.get_embedder() is None  # no key -> lexical fallback
    embeddings.get_embedder.cache_clear()


# --- batched cache: embed each unique text once -----------------------------
class CountingEmbedder(embeddings.Embedder):
    def __init__(self):
        self.calls = 0
        self.embedded = 0

    def embed(self, texts):
        self.calls += 1
        self.embedded += len(texts)
        return [[float(len(t)), 0.0] for t in texts]


def test_embed_texts_caches_and_batches(monkeypatch):
    counter = CountingEmbedder()
    monkeypatch.setattr(embeddings, "get_embedder", lambda: counter)
    embeddings.clear_cache()

    # First call embeds both; repeated titles are served from cache.
    embeddings.embed_texts(["btc", "eth"])
    embeddings.embed_texts(["btc", "fed"])   # only "fed" is new
    embeddings.embed_texts(["btc", "eth"])   # all cached

    assert counter.embedded == 3             # btc, eth, fed — each once
    assert counter.calls == 2                # third call hit cache entirely


def test_embed_texts_none_without_embedder(monkeypatch):
    monkeypatch.setattr(embeddings, "get_embedder", lambda: None)
    assert embeddings.embed_texts(["x"]) is None
