"""House probability — the server's own forecast, fused from every signal (K1).

Every module so far answers a narrow question: calibration (C3) corrects a price
against resolution history, meta-consensus (J4) weights venues by accuracy,
options (C7) cross-checks against real-money option chains, microstructure (C8)
reads the tape. Agents had to fuse these themselves. K1 does the fusion: one
number, ``house_probability``, with a confidence and a transparent breakdown of
what moved it — and, crucially, its disagreement with the raw market
(``edge_vs_market``), which is a forecast in its own right rather than an
arbitrage.

The fusion is an evidence-weighted blend of *independent* probability estimates
(each with a weight reflecting how much evidence stands behind it), plus a small,
capped directional tilt from informed-flow microstructure (a signal about
*direction*, not a probability). Confidence rewards agreement and evidence and
penalizes dispersion.

Pure/offline: callers pass already-computed signals (matching the style of
``adverse``/``metaconsensus``); this module only combines them. The scoring half
— recording each forecast and grading it at resolution with a public Brier — lives
in ``houseforecast``.
"""

from __future__ import annotations

# Microstructure is a directional read, not a probability; cap how far it can
# tilt the fused number so a noisy tape can't dominate the fundamentals.
_MICRO_MAX_TILT = 0.05


def _clamp01(x: float) -> float:
    return min(1.0, max(0.0, x))


def fuse(
    market_prob: float,
    calibration: dict | None = None,
    options: dict | None = None,
    meta: dict | None = None,
    micro: dict | None = None,
) -> dict:
    """Fuse the server's signals into one house probability for a market.

    ``calibration``  -> {calibrated_probability, samples, calibrated}   (C3)
    ``options``      -> {options_probability, ...}                      (C7)
    ``meta``         -> {estimate, venues:[{samples,...}], ...}         (J4)
    ``micro``        -> {momentum, informed_flow, ...}                  (C8)
    Any of them may be None/absent; the blend uses whatever is available.
    """
    market_prob = _clamp01(market_prob)
    components: list[dict] = []

    # 1. Calibrated market price — always present. Weight grows with how much
    #    resolution history informs the correction (and is never zero, so the
    #    market anchor always participates).
    if calibration and calibration.get("calibrated"):
        cal_p = _clamp01(float(calibration.get("calibrated_probability", market_prob)))
        cal_w = 1.0 + min(3.0, float(calibration.get("samples", 0)) / 50.0)
        components.append({"source": "calibrated_market", "probability": round(cal_p, 4),
                           "weight": round(cal_w, 4)})
    else:
        components.append({"source": "market", "probability": round(market_prob, 4),
                           "weight": 1.0})

    # 2. Options-implied (real money) — a strong independent anchor when present.
    if options and options.get("options_probability") is not None:
        op = _clamp01(float(options["options_probability"]))
        components.append({"source": "options_implied", "probability": round(op, 4),
                           "weight": 2.0})

    # 3. Cross-venue accuracy-weighted consensus (J4) — weight scales with the
    #    total resolved evidence behind the venues' accuracy estimates.
    if meta and meta.get("estimate") is not None:
        mp = _clamp01(float(meta["estimate"]))
        samples = sum(int(v.get("samples", 0)) for v in meta.get("venues", []))
        meta_w = 0.5 + min(2.0, samples / 40.0)
        components.append({"source": "meta_consensus", "probability": round(mp, 4),
                           "weight": round(meta_w, 4)})

    total_w = sum(c["weight"] for c in components)
    blended = sum(c["probability"] * c["weight"] for c in components) / total_w

    # 4. Microstructure tilt: informed flow nudges the estimate in its direction,
    #    bounded by _MICRO_MAX_TILT so the tape refines but never overrides.
    tilt = 0.0
    if micro and micro.get("informed_flow"):
        momentum = float(micro.get("momentum", 0.0))  # [-1, 1]
        tilt = max(-_MICRO_MAX_TILT, min(_MICRO_MAX_TILT, momentum * _MICRO_MAX_TILT))
    house = _clamp01(blended + tilt)

    # Confidence: high when the independent estimates agree (low dispersion) and
    # there is real evidence behind them; low when they scatter.
    dispersion = max((abs(c["probability"] - blended) for c in components), default=0.0)
    evidence = min(1.0, (total_w - 1.0) / 4.0)  # 0 when only the bare market anchor
    confidence = round(_clamp01((1.0 - dispersion * 2.0) * (0.4 + 0.6 * evidence)), 4)

    return {
        "house_probability": round(house, 4),
        "market_probability": round(market_prob, 4),
        # The server's disagreement with the market: a forecast, not an arb.
        "edge_vs_market": round(house - market_prob, 4),
        "confidence": confidence,
        "dispersion": round(dispersion, 4),
        "components": components,
        "micro_tilt": round(tilt, 4),
        "method": "evidence-weighted signal fusion (calibration + options + "
                  "meta-consensus + microstructure tilt)",
    }
