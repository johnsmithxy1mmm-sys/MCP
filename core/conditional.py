"""Conditional-probability / event-dependency graph (H2).

Markets price P(A) and P(B) separately, but agents reason conditionally: "if the
Fed cuts, what's P(recession)?" No prediction-market product answers that. We
estimate the market-implied **P(A | B)** two ways and combine them:

* **Logical** (exact): if A entails B, P(B|A) = 1; if A and B are mutually
  exclusive, P(B|A) = 0. These come from the event graph's structure.
* **Statistical** (a Gaussian copula): map each marginal to a latent normal, fit
  the latent correlation from the two markets' price co-movement, and read the
  joint P(A∩B) off the bivariate normal — then P(A|B) = P(A∩B) / P(B). At zero
  correlation this collapses to independence (P(A|B) = P(A)); at full correlation
  to the comonotone bound. It's an *estimate* (price correlation proxies the
  latent correlation), surfaced with that caveat.

Pure/stdlib math — no numpy. Bivariate normal CDF via the exact integral
representation, inverse-normal via Acklam's rational approximation.
"""

from __future__ import annotations

import math


def _phi(x: float) -> float:
    """Standard normal CDF."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _probit(p: float) -> float:
    """Inverse standard normal CDF (Acklam's approximation)."""
    p = min(1 - 1e-12, max(1e-12, p))
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
               ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p > phigh:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
               ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    q = p - 0.5
    r = q * q
    return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / \
           (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)


def _bvn_density(h: float, k: float, r: float) -> float:
    denom = 2 * math.pi * math.sqrt(max(1e-12, 1 - r * r))
    expo = -(h * h - 2 * r * h * k + k * k) / (2 * (1 - r * r))
    return math.exp(expo) / denom


def bvn_cdf(h: float, k: float, rho: float, steps: int = 64) -> float:
    """P(Z1 <= h, Z2 <= k) for standard bivariate normals with correlation rho.

    Uses Φ2(h,k,ρ) = Φ(h)Φ(k) + ∫_0^ρ φ2(h,k,r) dr (Simpson's rule).
    """
    rho = max(-0.999, min(0.999, rho))
    base = _phi(h) * _phi(k)
    if abs(rho) < 1e-9:
        return base
    n = steps if steps % 2 == 0 else steps + 1
    dr = rho / n
    total = _bvn_density(h, k, 0.0) + _bvn_density(h, k, rho)
    for i in range(1, n):
        total += (4 if i % 2 else 2) * _bvn_density(h, k, i * dr)
    return base + dr / 3.0 * total


def conditional(p_a: float, p_b: float, rho: float) -> dict:
    """Market-implied conditional probabilities from marginals + latent correlation.

    P(A|B) = P(A∩B)/P(B) with the joint from a Gaussian copula.
    """
    p_a = min(1.0, max(0.0, p_a))
    p_b = min(1.0, max(0.0, p_b))
    if p_a == 0 or p_b == 0:
        return {"joint": 0.0, "p_a_given_b": 0.0, "p_b_given_a": 0.0,
                "independent_joint": round(p_a * p_b, 4), "correlation": round(rho, 4)}
    z_a, z_b = _probit(p_a), _probit(p_b)
    joint = min(p_a, p_b, max(0.0, bvn_cdf(z_a, z_b, rho)))
    return {
        "joint": round(joint, 4),
        "p_a_given_b": round(joint / p_b, 4),
        "p_b_given_a": round(joint / p_a, 4),
        "independent_joint": round(p_a * p_b, 4),  # what it'd be if uncorrelated
        "correlation": round(rho, 4),
    }


def _pearson(xs: list[float], ys: list[float]) -> float:
    n = min(len(xs), len(ys))
    if n < 3:
        return 0.0
    xs, ys = xs[-n:], ys[-n:]
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    if vx <= 0 or vy <= 0:
        return 0.0
    return cov / (vx ** 0.5 * vy ** 0.5)


def pairwise(markets, history_getter, logical=None) -> list[dict]:
    """Conditional probabilities for every ordered pair among ``markets``.

    ``history_getter(venue, market_id) -> [PricePoint]`` supplies the series for
    the correlation estimate. ``logical`` optionally maps (a_id, b_id) -> exact
    P(b|a) (1 for entailment, 0 for exclusion) to override the estimate.
    """
    logical = logical or {}
    series = {m.market_id: [p.yes_price for p in history_getter(m.venue.value, m.market_id)]
              for m in markets}
    out: list[dict] = []
    for i in range(len(markets)):
        for j in range(len(markets)):
            if i == j:
                continue
            a, b = markets[i], markets[j]
            rho = _pearson(series.get(a.market_id, []), series.get(b.market_id, []))
            cond = conditional(a.yes_price, b.yes_price, rho)
            p_a_given_b = cond["p_a_given_b"]
            override = logical.get((b.market_id, a.market_id))  # P(a | b)
            if override is not None:
                p_a_given_b = round(override, 4)
            out.append({
                "a": a.market_id, "b": b.market_id,
                "p_a_given_b": p_a_given_b,
                "p_a": round(a.yes_price, 4), "p_b": round(b.yes_price, 4),
                "correlation": cond["correlation"],
                "source": "logical" if override is not None else "copula",
            })
    return out
