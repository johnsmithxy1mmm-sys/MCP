"""Semantic / hybrid matcher tests, driven by a mocked embedder.

Real embeddings need model weights (network to download), so we inject a fake
embedder that maps each title to a concept vector by keyword. This lets us prove
the semantic tier resolves the lexical "shared entity + shared year" false
positive without any model or network.
"""

from __future__ import annotations

import pytest

from core import algorithms, embeddings
from core.models import Market, Venue


# Concept-vector embedder: primary subject wins (ethereum before bitcoin).
_CONCEPTS = {
    "ethereum": [0.0, 1.0, 0.0],
    "bitcoin": [1.0, 0.0, 0.0],
    "btc": [1.0, 0.0, 0.0],
    "fed": [0.0, 0.0, 1.0],
}


class FakeEmbedder(embeddings.Embedder):
    def embed(self, texts):
        out = []
        for t in texts:
            low = t.lower()
            vec = [0.0, 0.0, 0.0]
            for kw, v in _CONCEPTS.items():  # dict order: ethereum first
                if kw in low:
                    vec = v
                    break
            out.append(vec)
        return out


def _mkt(venue, mid, title):
    return Market(venue=venue, market_id=mid, title=title, yes_price=0.5, no_price=0.5)


PM_BTC = _mkt(Venue.POLYMARKET, "pm-btc", "Will Bitcoin close above $100k at end of 2026?")
KX_BTC = _mkt(Venue.KALSHI, "kx-btc", "Bitcoin above $100,000 on Dec 31 2026")
KX_ETH = _mkt(Venue.KALSHI, "kx-eth", "Will Ethereum flip Bitcoin in 2026?")


@pytest.fixture
def fake_embedder(monkeypatch):
    monkeypatch.setattr(embeddings, "get_embedder", lambda: FakeEmbedder())


def test_semantic_similarity_separates_shared_entity(fake_embedder):
    same = algorithms.semantic_similarity(PM_BTC.title, KX_BTC.title)
    cross = algorithms.semantic_similarity(PM_BTC.title, KX_ETH.title)
    assert same == 1.0            # same concept
    assert cross == 0.0           # Bitcoin market vs Ethereum market
    assert same > cross


def test_lexical_tier_false_positives_the_eth_pair(monkeypatch):
    # Baseline: with a low lexical threshold, the ETH market false-pairs with BTC.
    monkeypatch.delenv("MATCHER", raising=False)
    pairs = algorithms.match_markets([PM_BTC, KX_BTC, KX_ETH], min_confidence=0.4)
    matched = {(p.a.market_id, p.b.market_id) for p in pairs}
    assert any("kx-eth" in ids for ids in matched)  # the false positive slips in


def test_semantic_tier_rejects_the_eth_false_positive(monkeypatch, fake_embedder):
    monkeypatch.setenv("MATCHER", "semantic")
    pairs = algorithms.match_markets([PM_BTC, KX_BTC, KX_ETH], min_confidence=0.6)
    ids = {frozenset((p.a.market_id, p.b.market_id)) for p in pairs}
    assert frozenset(("pm-btc", "kx-btc")) in ids       # true pair kept
    assert not any("kx-eth" in s for s in ids)          # false pair rejected


def test_hybrid_blends_lexical_and_semantic(monkeypatch, fake_embedder):
    monkeypatch.setenv("MATCHER", "hybrid")
    lex = algorithms.title_similarity(PM_BTC.title, KX_BTC.title)
    got = algorithms.similarity(PM_BTC.title, KX_BTC.title)
    assert got == round(0.5 * lex + 0.5 * 1.0, 3)


def test_semantic_falls_back_to_lexical_when_no_embedder(monkeypatch):
    monkeypatch.setattr(embeddings, "get_embedder", lambda: None)
    monkeypatch.setenv("MATCHER", "semantic")
    # No embedder -> similarity() returns the lexical score, server still works.
    assert algorithms.similarity(PM_BTC.title, KX_BTC.title) == algorithms.title_similarity(
        PM_BTC.title, KX_BTC.title
    )


def test_matcher_defaults_to_lexical(monkeypatch, fake_embedder):
    monkeypatch.delenv("MATCHER", raising=False)
    # Default mode ignores the embedder entirely.
    assert algorithms.similarity(PM_BTC.title, KX_BTC.title) == algorithms.title_similarity(
        PM_BTC.title, KX_BTC.title
    )
