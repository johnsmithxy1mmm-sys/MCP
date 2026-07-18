"""C4: strategy backtester over recorded history."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core.backtest import BacktestParams, simulate
from core.models import PricePoint, Venue


def _series(prices):
    t0 = datetime(2026, 6, 1, tzinfo=timezone.utc)
    return [PricePoint(ts=t0 + timedelta(hours=i), yes_price=p) for i, p in enumerate(prices)]


def test_insufficient_history():
    out = simulate(_series([0.5, 0.5, 0.5]), BacktestParams(window=10))
    assert out["trades"] == 0
    assert "insufficient" in out["note"]


def test_mean_reversion_captures_a_dip():
    # Flat at 0.60, a sharp dip to 0.50, then recovery — one profitable round-trip.
    prices = [0.60] * 10 + [0.50, 0.52, 0.58, 0.60, 0.60]
    out = simulate(_series(prices), BacktestParams(
        entry_edge=0.05, exit_edge=0.01, window=10, size_usd=1000,
        slippage_bps=0, venue=Venue.POLYMARKET,  # Polymarket has no venue fee
    ))
    assert out["trades"] == 1
    assert out["hit_rate"] == 1.0
    assert out["total_return"] > 0
    assert out["final_equity_usd"] > 1000


def test_costs_make_marginal_trades_unprofitable():
    # Dip to 0.50, only a tiny recovery to 0.52 (forced exit at series end).
    prices = [0.60] * 10 + [0.50, 0.52]
    # Heavy slippage (5% each fill) turns the tiny move into a net loss.
    out = simulate(_series(prices), BacktestParams(
        entry_edge=0.05, exit_edge=0.01, window=10, size_usd=1000,
        slippage_bps=500, venue=Venue.POLYMARKET,
    ))
    assert out["trades"] == 1
    assert out["avg_return_per_trade"] < 0  # eaten by slippage


def test_no_trades_when_price_never_dips():
    out = simulate(_series([0.60] * 20), BacktestParams(entry_edge=0.05, window=10))
    assert out["trades"] == 0
    assert "no trades" in out["note"]


@pytest.mark.asyncio
async def test_simulate_strategy_tool(client):
    r = (await client.call_tool("simulate_strategy", {
        "venue": "polymarket", "market_id": "pm-btc-100k-2026",
        "from_ts": "2026-06-01T00:00:00Z", "to_ts": "2026-07-01T00:00:00Z",
        "entry_edge": 0.03, "window": 3,
    })).data
    assert "result" in r
    assert r["params"]["window"] == 3


@pytest.mark.asyncio
async def test_simulate_strategy_unknown_venue(client):
    r = (await client.call_tool("simulate_strategy", {
        "venue": "polymrket",  # typo must error, not silently map to another venue
        "market_id": "m", "from_ts": "2026-06-01T00:00:00Z", "to_ts": "2026-07-01T00:00:00Z",
    })).data
    assert r["error"] == "unknown_venue"
    assert "polymarket" in r["hint"]


@pytest.mark.asyncio
async def test_simulate_strategy_bad_timestamp(client):
    r = (await client.call_tool("simulate_strategy", {
        "venue": "polymarket", "market_id": "pm-btc-100k-2026",
        "from_ts": "nope", "to_ts": "2026-07-01T00:00:00Z",
    })).data
    assert r["error"] == "bad_timestamp"
