"""Online probability calibration — the self-growing moat (C3).

A market price is a probability *claim*; our resolution history says how those
claims actually turned out. Calibration turns that history into a correction:
"when this server surfaced 0.70, the event resolved YES ~64% of the time." That
correction is (a) a forecast that differs from the raw market — a source of edge,
not just arbitrage — and (b) a moat that *compounds*: it's built from a
resolution record no competitor has, and it sharpens every day the server runs.

Method: bin surfaced implied-probabilities, take the empirical resolution rate per
bin, enforce monotonicity with Pool-Adjacent-Violators isotonic regression
(calibration must be non-decreasing), then linearly interpolate. Below
``CALIBRATION_MIN_SAMPLES`` resolved outcomes the calibrator is the identity —
we never "correct" a price on too little evidence.

Pure/offline; trains from the reconciliation store.

  CALIBRATION_MIN_SAMPLES  resolved outcomes required before correcting (default 30)
  CALIBRATION_BINS         number of probability bins (default 10)
"""

from __future__ import annotations

import os
import threading


def _pav(values: list[float], weights: list[float]) -> list[float]:
    """Pool-Adjacent-Violators: nearest non-decreasing fit (weighted L2)."""
    # Each stack entry: [weighted_sum, weight, count].
    stack: list[list[float]] = []
    for v, w in zip(values, weights):
        cur = [v * w, w, 1]
        while stack and stack[-1][0] / stack[-1][1] >= cur[0] / cur[1]:
            prev = stack.pop()
            cur = [prev[0] + cur[0], prev[1] + cur[1], prev[2] + cur[2]]
        stack.append(cur)
    out: list[float] = []
    for weighted_sum, weight, count in stack:
        out.extend([weighted_sum / weight] * int(count))
    return out


class Calibrator:
    """Maps a raw probability to a history-calibrated one (monotone, clamped)."""

    def __init__(self, centers: list[float], calibrated: list[float], n: int):
        self._centers = centers
        self._calibrated = calibrated
        self.n = n  # training sample count (0 => identity calibrator)

    @property
    def is_identity(self) -> bool:
        return not self._centers

    def calibrate(self, p: float) -> float:
        if self.is_identity:
            return round(p, 4)
        centers, cal = self._centers, self._calibrated
        if p <= centers[0]:
            return round(cal[0], 4)
        if p >= centers[-1]:
            return round(cal[-1], 4)
        for i in range(1, len(centers)):
            if p <= centers[i]:
                lo, hi = centers[i - 1], centers[i]
                frac = (p - lo) / (hi - lo) if hi > lo else 0.0
                return round(cal[i - 1] + frac * (cal[i] - cal[i - 1]), 4)
        return round(cal[-1], 4)


def fit_calibrator(samples: list[tuple[float, int]]) -> Calibrator:
    """Fit a Calibrator from (implied_prob, outcome) pairs."""
    min_samples = int(os.getenv("CALIBRATION_MIN_SAMPLES", "30"))
    if len(samples) < min_samples:
        return Calibrator([], [], len(samples))  # not enough evidence -> identity
    n_bins = max(2, int(os.getenv("CALIBRATION_BINS", "10")))
    sums = [0.0] * n_bins
    counts = [0] * n_bins
    for prob, outcome in samples:
        idx = min(n_bins - 1, max(0, int(prob * n_bins)))
        sums[idx] += outcome
        counts[idx] += 1
    centers: list[float] = []
    rates: list[float] = []
    weights: list[float] = []
    for i in range(n_bins):
        if counts[i] == 0:
            continue
        centers.append((i + 0.5) / n_bins)
        rates.append(sums[i] / counts[i])
        weights.append(float(counts[i]))
    if len(centers) < 2:
        return Calibrator([], [], len(samples))
    calibrated = _pav(rates, weights)
    return Calibrator(centers, calibrated, len(samples))


# --- process-cached calibrator (rebuilt as new outcomes resolve) ------------
_LOCK = threading.Lock()
_CACHE: dict[str, tuple[int, Calibrator]] = {}


def get_calibrator(category: str | None = None) -> Calibrator:
    """Calibrator trained on current resolution history. Cached and rebuilt only
    when the resolved-sample count changes (cheap on the hot path)."""
    from .reconciliation import get_reconciliation

    samples = get_reconciliation().calibration_samples(category)
    key = category or "*"
    signature = len(samples)
    with _LOCK:
        cached = _CACHE.get(key)
        if cached is not None and cached[0] == signature:
            return cached[1]
        calibrator = fit_calibrator(samples)
        _CACHE[key] = (signature, calibrator)
        return calibrator


def clear_cache() -> None:
    with _LOCK:
        _CACHE.clear()


def calibrate_probability(p: float, category: str | None = None) -> dict:
    """Calibrated probability + how much history informed it (for a response)."""
    calibrator = get_calibrator(category)
    return {
        "calibrated_probability": calibrator.calibrate(p),
        "raw_probability": round(p, 4),
        "samples": calibrator.n,
        "calibrated": not calibrator.is_identity,
    }
