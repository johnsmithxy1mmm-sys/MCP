"""Fuzzy matcher tests (offline, lexical tier)."""

from __future__ import annotations

from core.algorithms import match_markets, title_similarity
from core.models import Market, Venue


def _mkt(venue, mid, title, yes=0.5):
    return Market(venue=venue, market_id=mid, title=title, yes_price=yes, no_price=round(1 - yes, 4))


def test_number_unit_normalization_reinforces_match():
    # '$100k' and '$100,000' must be treated as the same number.
    high = title_similarity("BTC above $100k in 2026", "BTC above $100,000 in 2026")
    low = title_similarity("BTC above $100k in 2026", "BTC above $5 in 2026")
    assert high > low
    assert high > 0.6


def test_true_pairs_score_above_cross_topic():
    btc = title_similarity(
        "Will Bitcoin close above $100k at end of 2026?",
        "Bitcoin above $100,000 on Dec 31 2026",
    )
    cross_topic = title_similarity(
        "Will Bitcoin close above $100k at end of 2026?",
        "Will the Fed cut rates at the September meeting?",
    )
    assert btc > 0.6
    assert cross_topic < 0.25
    assert btc > cross_topic


def test_match_markets_pairs_same_event_across_venues():
    markets = [
        _mkt(Venue.POLYMARKET, "pm-btc", "Will Bitcoin close above $100k at end of 2026?", 0.71),
        _mkt(Venue.KALSHI, "kx-btc", "Bitcoin above $100,000 on Dec 31 2026", 0.66),
        _mkt(Venue.POLYMARKET, "pm-fed", "Will the Fed cut rates at the September meeting?", 0.40),
    ]
    pairs = match_markets(markets)
    assert len(pairs) == 1
    p = pairs[0]
    assert {p.a.venue, p.b.venue} == {Venue.POLYMARKET, Venue.KALSHI}
    assert {p.a.market_id, p.b.market_id} == {"pm-btc", "kx-btc"}
    assert p.confidence >= 0.45


def test_same_venue_never_paired():
    markets = [
        _mkt(Venue.POLYMARKET, "a", "Bitcoin above $100,000 in 2026"),
        _mkt(Venue.POLYMARKET, "b", "Bitcoin above $100,000 in 2026"),
    ]
    assert match_markets(markets) == []


def test_empty_titles_score_zero():
    assert title_similarity("", "anything") == 0.0
