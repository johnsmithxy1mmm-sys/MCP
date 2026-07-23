"""J2: continuous implied PDF — lognormal fit over the whole axis."""

from __future__ import annotations

import json
import math

import pytest

from core.distribution import (
    _survival_points,
    fit_lognormal,
    prob_in_range,
    quantile,
)
from core.models import Market, Venue


def _lognormal_ladder(mu, sigma, strikes):
    """Build threshold markets whose YES prices are the TRUE lognormal survival."""
    def survival(k):
        return 1.0 - 0.5 * (1 + math.erf((math.log(k) - mu) / (sigma * math.sqrt(2))))
    return [Market(venue=Venue.POLYMARKET, market_id=f"btc-{k}",
                   title=f"Will Bitcoin close above ${k} at end of 2026?",
                   yes_price=round(survival(k), 4), no_price=round(1 - survival(k), 4),
                   category="crypto") for k in strikes]


def test_fit_recovers_lognormal_params():
    mu, sigma = math.log(120_000), 0.4
    points = _survival_points(_lognormal_ladder(mu, sigma, [80_000, 100_000, 150_000, 200_000, 250_000]))
    fit = fit_lognormal(points)
    assert fit is not None
    assert fit["mu"] == pytest.approx(mu, abs=0.05)
    assert fit["sigma"] == pytest.approx(sigma, abs=0.05)
    assert fit["median"] == pytest.approx(120_000, rel=0.05)


def test_prob_in_range_and_quantiles():
    mu, sigma = math.log(120_000), 0.4
    points = _survival_points(_lognormal_ladder(mu, sigma, [80_000, 100_000, 150_000, 200_000, 250_000]))
    fit = fit_lognormal(points)
    # Full support integrates to ~1; a sub-range is a proper fraction.
    assert prob_in_range(fit, None, None) == pytest.approx(1.0, abs=0.01)
    p = prob_in_range(fit, 100_000, 150_000)
    assert 0.0 < p < 1.0
    # Quantiles are ordered and centered on the median.
    assert quantile(fit, 0.1) < quantile(fit, 0.5) < quantile(fit, 0.9)
    assert quantile(fit, 0.5) == pytest.approx(120_000, rel=0.05)


def test_fit_none_on_degenerate_ladder():
    # A flat (non-monotone) ladder can't determine sigma > 0.
    markets = [Market(venue=Venue.POLYMARKET, market_id=str(k),
                      title=f"Bitcoin above ${k} at end of 2026", yes_price=0.5, no_price=0.5)
               for k in (100_000, 150_000)]
    assert fit_lognormal(_survival_points(markets)) is None


@pytest.mark.asyncio
async def test_distribution_resource_has_continuous(client):
    r = await client.read_resource("distribution://bitcoin")
    data = json.loads(r[0].text)
    assert data["found"] is True
    assert "continuous" in data
    c = data["continuous"]
    assert c["model"] == "lognormal"
    assert c["quantiles"]["p10"] < c["quantiles"]["p90"]
