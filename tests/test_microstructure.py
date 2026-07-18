"""C8: price microstructure / informed-flow proxy."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core.microstructure import compute
from core.models import PricePoint


def _series(prices):
    t0 = datetime(2026, 6, 1, tzinfo=timezone.utc)
    return [PricePoint(ts=t0 + timedelta(minutes=i), yes_price=p) for i, p in enumerate(prices)]


def test_none_on_short_series():
    assert compute(_series([0.5, 0.5])) is None


def test_one_sided_uptape_flags_informed_flow():
    out = compute(_series([0.50, 0.52, 0.54, 0.56, 0.58, 0.60]))
    assert out["uptick_ratio"] == 1.0
    assert out["momentum"] == 1.0          # fully one-sided up
    assert out["direction"] == "up"
    assert out["drift"] > 0
    assert out["informed_flow"] is True


def test_choppy_tape_is_not_informed():
    out = compute(_series([0.50, 0.51, 0.50, 0.51, 0.50, 0.51, 0.50]))
    assert abs(out["momentum"]) < 0.5
    assert out["informed_flow"] is False


def test_downtape_direction():
    out = compute(_series([0.60, 0.58, 0.56, 0.54, 0.52]))
    assert out["direction"] == "down"
    assert out["momentum"] == -1.0


def test_window_truncates(monkeypatch):
    out = compute(_series([0.1] * 50 + [0.2, 0.3, 0.4]), window=3)
    assert out["samples"] == 3


@pytest.mark.asyncio
async def test_evaluate_market_includes_microstructure(client):
    r = (await client.call_tool(
        "evaluate_market", {"venue": "polymarket", "market_id": "pm-btc-100k-2026"}
    )).data
    # mock history has enough points -> a microstructure block is present.
    assert "microstructure" in r
    assert "momentum" in r["microstructure"]
