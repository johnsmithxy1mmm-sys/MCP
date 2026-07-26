"""K5: market-maker advisor — quote placement + adverse-selection guard."""

from __future__ import annotations

import json

import pytest

from core.maker import advise


def test_calm_market_gives_tight_symmetric_quotes():
    a = advise(0.60, best_bid=0.59, best_ask=0.61, momentum=0.0,
               informed_flow=False, realized_vol=0.002)
    assert a["recommended_bid"] < 0.60 < a["recommended_ask"]
    assert a["pull_quotes"] is False
    assert a["skew"] == 0.0
    assert a["expected_spread_capture"] > 0


def test_informed_flow_widens_and_skews_up():
    calm = advise(0.60, best_bid=0.59, best_ask=0.61, momentum=0.0, informed_flow=False)
    hot = advise(0.60, best_bid=0.59, best_ask=0.61, momentum=0.8,
                 informed_flow=True, realized_vol=0.02)
    assert hot["half_spread"] > calm["half_spread"]   # widen under adverse pressure
    assert hot["skew"] > 0                             # lean up with informed buying
    assert hot["adverse_pressure"] > calm["adverse_pressure"]


def test_pull_quotes_when_adverse_pressure_high(monkeypatch):
    monkeypatch.setenv("MAKER_PULL_ADVERSE", "0.5")
    a = advise(0.60, best_bid=0.55, best_ask=0.65, momentum=-0.9,
               informed_flow=True, realized_vol=0.05, quality_flags=["wide_spread"])
    assert a["adverse_pressure"] >= 0.5 and a["pull_quotes"] is True


def test_half_spread_respects_floor(monkeypatch):
    monkeypatch.setenv("MAKER_MIN_HALF_SPREAD", "0.02")
    # A razor-thin book can't push the quote below the configured floor.
    a = advise(0.50, best_bid=0.4999, best_ask=0.5001, momentum=0.0, informed_flow=False)
    assert a["half_spread"] >= 0.02


def test_quotes_stay_in_unit_interval():
    a = advise(0.98, best_bid=0.97, best_ask=0.99, momentum=1.0,
               informed_flow=True, realized_vol=0.1)
    assert 0.0 <= a["recommended_bid"] <= 1.0
    assert 0.0 <= a["recommended_ask"] <= 1.0


# --- through deps + MCP surface ---------------------------------------------
def test_maker_advice_via_deps():
    from predmarket_mcp import deps

    assert deps.maker_advice("kalshi", "nope") is None
    adv = deps.maker_advice("kalshi", "kx-btc-100k-eoy26")
    assert adv is not None
    assert "recommended_bid" in adv and "recommended_ask" in adv
    assert adv["recommended_bid"] < adv["recommended_ask"]


@pytest.mark.asyncio
async def test_maker_resource_shape(client):
    mk = json.loads((await client.read_resource("maker://kalshi/kx-btc-100k-eoy26"))[0].text)
    assert {"recommended_bid", "recommended_ask", "pull_quotes", "half_spread"} <= set(mk)
    assert "as_of" in mk


@pytest.mark.asyncio
async def test_maker_resource_unknown_market(client):
    mk = json.loads((await client.read_resource("maker://kalshi/nope"))[0].text)
    assert mk["error"] == "market_not_found"
