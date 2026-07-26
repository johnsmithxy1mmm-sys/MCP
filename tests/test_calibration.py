"""C3: online probability calibration."""

from __future__ import annotations

import pytest

from core import calibration
from core.calibration import _pav, fit_calibrator


# --- isotonic core ----------------------------------------------------------
def test_pav_enforces_monotonicity():
    # Input dips (0.8 then 0.4) -> PAV pools them to a single non-decreasing run.
    fitted = _pav([0.2, 0.8, 0.4, 0.9], [1, 1, 1, 1])
    assert fitted == sorted(fitted)          # non-decreasing
    assert fitted[1] == fitted[2] == pytest.approx(0.6)  # 0.8 & 0.4 pooled to 0.6


# --- fit --------------------------------------------------------------------
def test_identity_below_min_samples(monkeypatch):
    monkeypatch.setenv("CALIBRATION_MIN_SAMPLES", "30")
    cal = fit_calibrator([(0.7, 1), (0.7, 0)])  # only 2 samples
    assert cal.is_identity
    assert cal.calibrate(0.7) == 0.7


def test_calibrator_corrects_overconfident_prices(monkeypatch):
    monkeypatch.setenv("CALIBRATION_MIN_SAMPLES", "10")
    monkeypatch.setenv("CALIBRATION_BINS", "10")
    # Build history where surfaced 0.70 only resolves YES 40% of the time.
    samples = [(0.7, 1)] * 4 + [(0.7, 0)] * 6      # bin around 0.7 -> rate 0.4
    samples += [(0.2, 1)] * 2 + [(0.2, 0)] * 8     # bin around 0.2 -> rate 0.2
    cal = fit_calibrator(samples)
    assert not cal.is_identity
    assert cal.calibrate(0.7) < 0.7                # overconfidence corrected down
    assert cal.calibrate(0.7) == pytest.approx(0.4, abs=0.05)


def test_calibrate_is_monotone_and_clamped(monkeypatch):
    monkeypatch.setenv("CALIBRATION_MIN_SAMPLES", "4")
    samples = [(0.1, 0), (0.3, 0), (0.6, 1), (0.9, 1)]
    cal = fit_calibrator(samples)
    xs = [i / 20 for i in range(21)]
    ys = [cal.calibrate(x) for x in xs]
    assert ys == sorted(ys)                        # monotone non-decreasing
    assert all(0.0 <= y <= 1.0 for y in ys)        # clamped to [0,1]


# --- integration via the store ----------------------------------------------
def test_get_calibrator_trains_from_store(monkeypatch, tmp_path):
    monkeypatch.setenv("CALIBRATION_MIN_SAMPLES", "5")
    from core.reconciliation import ReconciliationStore
    import core.reconciliation as recon

    store = ReconciliationStore(db_url=f"sqlite:///{tmp_path / 'r.db'}")
    monkeypatch.setattr(recon, "get_reconciliation", lambda: store)

    def seed(conn, prefix, prob, n, yes):
        for i in range(n):
            conn.execute(
                "INSERT INTO flagged (opp_id, ts, kind, title, predicted_edge, "
                "implied_prob, legs, resolved, outcome) VALUES (?,?,?,?,?,?,?,1,?)",
                (f"{prefix}{i}", "t", "bundle", "m", 0.0, prob, "[]", 1 if i < yes else 0),
            )

    with store._connect() as conn:  # two bins: 0.2 resolves 20%, 0.8 resolves 30%
        seed(conn, "lo", 0.2, 10, 2)
        seed(conn, "hi", 0.8, 10, 3)
    calibration.clear_cache()
    cal = calibration.get_calibrator()
    assert cal.n == 20
    assert cal.calibrate(0.8) < 0.8  # 0.8 resolved YES only 30% -> corrected down


@pytest.mark.asyncio
async def test_evaluate_market_returns_calibration(client):
    r = (await client.call_tool(
        "evaluate_market", {"venue": "polymarket", "market_id": "pm-btc-100k-2026"}
    )).data
    assert "calibration" in r
    assert "calibrated_probability" in r["calibration"]
    assert "samples" in r["calibration"]
