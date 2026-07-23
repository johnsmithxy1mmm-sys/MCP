"""K1: house probability — signal fusion + publicly-graded forecast store."""

from __future__ import annotations

import pytest

from core.houseview import fuse
from core.houseforecast import HouseForecastStore


# --- fusion -----------------------------------------------------------------
def test_fuse_blends_signals_and_reports_edge():
    v = fuse(
        0.60,
        calibration={"calibrated": True, "calibrated_probability": 0.64, "samples": 100},
        options={"options_probability": 0.70},
        meta={"estimate": 0.62, "venues": [{"samples": 40}]},
        micro={"informed_flow": True, "momentum": 0.5},
    )
    assert 0.60 < v["house_probability"] <= 1.0          # pulled up by the signals
    assert v["edge_vs_market"] == round(v["house_probability"] - 0.60, 4)
    sources = {c["source"] for c in v["components"]}
    assert {"calibrated_market", "options_implied", "meta_consensus"} <= sources
    assert v["confidence"] > 0.5


def test_fuse_market_only_is_lower_confidence():
    # No calibration/options/meta -> only the bare market anchor -> low confidence.
    v = fuse(0.55)
    assert v["house_probability"] == 0.55
    assert v["components"] == [{"source": "market", "probability": 0.55, "weight": 1.0}]
    assert v["confidence"] <= 0.5


def test_micro_tilt_is_bounded():
    # A maximally one-sided tape can move the estimate at most _MICRO_MAX_TILT.
    up = fuse(0.50, micro={"informed_flow": True, "momentum": 1.0})
    assert up["micro_tilt"] == pytest.approx(0.05, abs=1e-9)
    assert up["house_probability"] == pytest.approx(0.55, abs=1e-9)
    down = fuse(0.50, micro={"informed_flow": True, "momentum": -1.0})
    assert down["house_probability"] == pytest.approx(0.45, abs=1e-9)


def test_no_informed_flow_no_tilt():
    v = fuse(0.50, micro={"informed_flow": False, "momentum": 1.0})
    assert v["micro_tilt"] == 0.0


# --- forecast store ---------------------------------------------------------
def test_forecast_upserts_latest_and_freezes_on_resolve(tmp_path):
    store = HouseForecastStore(f"sqlite:///{tmp_path / 'h.db'}")
    store.record("m1", "kalshi", "t", 0.70, 0.60)
    store.record("m1", "kalshi", "t", 0.72, 0.61)   # upsert: latest wins
    store.resolve({"m1": 1})
    store.record("m1", "kalshi", "t", 0.90, 0.90)   # frozen: must NOT overwrite
    m = store.metrics()
    assert m["resolved_count"] == 1 and m["pending_count"] == 0
    # house Brier uses the frozen 0.72, not the post-hoc 0.90.
    assert m["house_brier"] == pytest.approx((0.72 - 1) ** 2, abs=1e-6)


def test_brier_edge_positive_when_house_beats_market(tmp_path):
    store = HouseForecastStore(f"sqlite:///{tmp_path / 'h.db'}")
    # House is more confident (and correct) than the market on each resolved event.
    store.record("a", "kalshi", "t", 0.80, 0.60); store.resolve({"a": 1})
    store.record("b", "kalshi", "t", 0.20, 0.40); store.resolve({"b": 0})
    m = store.metrics()
    assert m["brier_edge"] > 0                # market_brier - house_brier > 0
    assert m["house_hit_rate"] == 1.0


def test_metrics_empty_is_neutral(tmp_path):
    store = HouseForecastStore(f"sqlite:///{tmp_path / 'h.db'}")
    m = store.metrics()
    assert m == {"resolved_count": 0, "pending_count": 0, "house_brier": None,
                 "market_brier": None, "brier_edge": None, "house_hit_rate": None}


# --- integration through deps + tools ---------------------------------------
def test_house_view_records_and_resolver_grades():
    from predmarket_mcp import deps

    view = deps.house_view("kalshi", "kx-btc-100k-eoy26")
    assert view is not None and "house_probability" in view
    assert deps.house_forecast_metrics()["pending_count"] == 1
    deps.resolve_outcomes({"kx-btc-100k-eoy26": 1})   # autonomous resolver path
    m = deps.house_forecast_metrics()
    assert m["resolved_count"] == 1 and m["house_brier"] is not None


@pytest.mark.asyncio
async def test_evaluate_market_carries_house_block(client):
    r = (await client.call_tool(
        "evaluate_market", {"venue": "kalshi", "market_id": "kx-btc-100k-eoy26"})).data
    assert "house" in r
    assert 0.0 <= r["house"]["house_probability"] <= 1.0


@pytest.mark.asyncio
async def test_track_record_reports_house_scorecard(client):
    r = (await client.call_tool("track_record", {})).data
    assert "house_forecast" in r
    assert {"house_brier", "market_brier", "brier_edge"} <= set(r["house_forecast"])
