"""H2: conditional-probability graph — Gaussian copula + logical overrides."""

from __future__ import annotations

import json

import pytest

from core.conditional import bvn_cdf, conditional, pairwise, _phi, _probit
from core.models import Market, PricePoint, Venue
from datetime import datetime, timedelta, timezone


# --- copula math ------------------------------------------------------------
def test_probit_inverts_phi():
    for p in (0.1, 0.3, 0.5, 0.7, 0.9):
        assert _phi(_probit(p)) == pytest.approx(p, abs=1e-4)


def test_bvn_independence_and_comonotone():
    h = k = _probit(0.6)
    assert bvn_cdf(h, k, 0.0) == pytest.approx(0.6 * 0.6, abs=1e-3)      # independent
    assert bvn_cdf(h, k, 0.999) == pytest.approx(0.6, abs=1e-2)          # comonotone -> min


def test_conditional_independence():
    # rho=0 -> P(A|B) == P(A) (knowing B tells you nothing).
    c = conditional(0.4, 0.7, 0.0)
    assert c["p_a_given_b"] == pytest.approx(0.4, abs=0.01)
    assert c["joint"] == pytest.approx(0.28, abs=0.01)


def test_conditional_positive_correlation_raises_it():
    base = conditional(0.4, 0.7, 0.0)["p_a_given_b"]
    corr = conditional(0.4, 0.7, 0.8)["p_a_given_b"]
    assert corr > base  # B makes A more likely when positively correlated


def test_conditional_monotone_in_correlation():
    vals = [conditional(0.4, 0.6, r)["p_a_given_b"] for r in (-0.8, -0.3, 0.0, 0.3, 0.8)]
    assert vals == sorted(vals)


# --- pairwise with history + logical overrides ------------------------------
def _mkt(mid, yes, group=None):
    return Market(venue=Venue.POLYMARKET, market_id=mid, title=mid, yes_price=yes,
                  no_price=round(1 - yes, 4), category="crypto", event_group=group)


def _hist(series):
    def getter(venue, mid):
        t0 = datetime(2026, 6, 1, tzinfo=timezone.utc)
        return [PricePoint(ts=t0 + timedelta(minutes=i), yes_price=p)
                for i, p in enumerate(series.get(mid, []))]
    return getter


def test_pairwise_uses_history_correlation():
    markets = [_mkt("a", 0.4), _mkt("b", 0.6)]
    getter = _hist({"a": [0.3, 0.4, 0.5, 0.6], "b": [0.5, 0.6, 0.7, 0.8]})  # corr +1
    out = pairwise(markets, getter)
    ab = next(r for r in out if r["a"] == "a" and r["b"] == "b")
    assert ab["correlation"] > 0.9
    assert ab["p_a_given_b"] > 0.4          # positive dependence lifts it
    assert ab["source"] == "copula"


def test_logical_override_wins():
    markets = [_mkt("weak", 0.5), _mkt("strong", 0.3)]
    # strong implies weak -> P(weak | strong) = 1 regardless of the estimate.
    out = pairwise(markets, _hist({}), logical={("strong", "weak"): 1.0})
    r = next(r for r in out if r["a"] == "weak" and r["b"] == "strong")
    assert r["p_a_given_b"] == 1.0 and r["source"] == "logical"


# --- resource ---------------------------------------------------------------
@pytest.mark.asyncio
async def test_conditional_resource(client):
    r = await client.read_resource("conditional://bitcoin")
    data = json.loads(r[0].text)
    assert data["found"] is True
    assert data["conditionals"]
    for c in data["conditionals"]:
        assert 0.0 <= c["p_a_given_b"] <= 1.0
