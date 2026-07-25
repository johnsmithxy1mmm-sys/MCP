"""C7: options-implied probability (Breeden-Litzenberger) + Deribit provider."""

from __future__ import annotations

import httpx
import pytest

from core import options
from core.options import DeribitProvider, divergence, prob_above


# --- Breeden-Litzenberger math ----------------------------------------------
def test_prob_above_from_call_slope():
    # Call prices falling with strike; local slope -> exceedance probability.
    # C(90k)=12000, C(100k)=6000, C(110k)=2000 (USD). Around 100k the central
    # slope is (2000-12000)/(110k-90k) = -0.5 -> P(S>100k) = 0.5.
    chain = [(90_000, 12_000.0), (100_000, 6_000.0), (110_000, 2_000.0)]
    p = prob_above(chain, 100_000)
    assert p == pytest.approx(0.5, abs=0.01)


def test_prob_above_is_bounded_and_monotone():
    chain = [(80_000, 20_000.0), (100_000, 8_000.0), (120_000, 1_000.0)]
    p_lo = prob_above(chain, 80_000)
    p_hi = prob_above(chain, 120_000)
    assert 0.0 <= p_hi <= p_lo <= 1.0  # exceedance falls as the strike rises


def test_prob_above_none_on_sparse_chain():
    assert prob_above([(100_000, 5_000.0)], 100_000) is None


def test_divergence_direction():
    d = divergence(0.70, 0.60)
    assert d["divergence"] == pytest.approx(0.10)
    assert d["direction"] == "market_rich"
    assert divergence(0.50, 0.60)["direction"] == "market_cheap"


# --- provider with mocked HTTP ----------------------------------------------
def test_deribit_provider_parses_chain():
    book = {"result": [
        {"instrument_name": "BTC-27DEC26-90000-C", "mark_price": 0.12, "underlying_price": 100_000},
        {"instrument_name": "BTC-27DEC26-100000-C", "mark_price": 0.06, "underlying_price": 100_000},
        {"instrument_name": "BTC-27DEC26-110000-C", "mark_price": 0.02, "underlying_price": 100_000},
        {"instrument_name": "BTC-27DEC26-100000-P", "mark_price": 0.05, "underlying_price": 100_000},  # put ignored
    ]}

    def factory():
        return httpx.Client(base_url="https://d",
                            transport=httpx.MockTransport(lambda r: httpx.Response(200, json=book)))

    prov = DeribitProvider(client_factory=factory)
    chain = prov.call_chain("BTC")
    assert len(chain) == 3  # puts excluded
    p = prov.implied_probability("BTC", 100_000)
    assert 0.0 <= p <= 1.0


def test_provider_degrades_on_http_error():
    def factory():
        return httpx.Client(base_url="https://d",
                            transport=httpx.MockTransport(lambda r: httpx.Response(503)))

    prov = DeribitProvider(client_factory=factory)
    assert prov.call_chain("BTC") == []
    assert prov.implied_probability("BTC", 100_000) is None


def test_get_provider_off_by_default(monkeypatch):
    monkeypatch.delenv("OPTIONS_ENABLED", raising=False)
    assert options.get_provider() is None


# --- integration: evaluate_market carries options divergence when enabled ----
@pytest.mark.asyncio
async def test_evaluate_market_options_divergence(client, monkeypatch):

    class FakeProvider:
        def implied_probability(self, currency, strike):
            return 0.55

    monkeypatch.setattr("core.options.get_provider", lambda: FakeProvider())
    r = (await client.call_tool(
        "evaluate_market", {"venue": "polymarket", "market_id": "pm-btc-100k-2026"}
    )).data
    # pm-btc-100k-2026 title parses to BTC strike 100000 -> divergence present.
    assert "options" in r
    assert r["options"]["currency"] == "BTC"
    assert "divergence" in r["options"]
