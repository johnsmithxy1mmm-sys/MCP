"""J3: Bayesian inference chains over the dependency graph."""

from __future__ import annotations

import json

import pytest

from core.inference import infer_target, posterior, weight_of_evidence


def test_weight_of_evidence_sign():
    # Evidence that raises P(A) above the prior contributes positive weight.
    assert weight_of_evidence(0.5, 0.7) > 0
    assert weight_of_evidence(0.5, 0.3) < 0
    assert weight_of_evidence(0.5, 0.5) == pytest.approx(0.0, abs=1e-9)


def test_corroborating_evidence_compounds():
    # Two independent pieces each pointing to 0.7 push the posterior past 0.7.
    one = posterior(0.5, [0.7])
    two = posterior(0.5, [0.7, 0.7])
    assert 0.7 == pytest.approx(one, abs=0.001)   # single piece recovers the conditional
    assert two > one > 0.5


def test_contradictory_evidence_cancels():
    # Equal-and-opposite evidence returns to the prior.
    assert posterior(0.5, [0.7, 0.3]) == pytest.approx(0.5, abs=0.01)


def test_mutually_exclusive_evidence_kills_it():
    # A sibling observed true (P(A|sibling)=0) drives the posterior to ~0.
    assert posterior(0.4, [0.0]) < 0.01


def test_certain_evidence_confirms():
    # P(A|E)=1 for observed E drives the posterior to ~1.
    assert posterior(0.4, [1.0]) > 0.99


def test_infer_target_shape():
    out = infer_target(0.5, [{"conditional": 0.7, "label": "B"},
                             {"conditional": 0.6, "label": "C"}])
    assert out["prior"] == 0.5
    assert out["posterior"] > 0.5
    assert out["evidence_count"] == 2
    assert "conditionally independent" in out["note"]


@pytest.mark.asyncio
async def test_conditional_resource_has_bayesian_updates(client):
    r = await client.read_resource("conditional://bitcoin")
    data = json.loads(r[0].text)
    assert "bayesian_updates" in data
    for u in data["bayesian_updates"]:
        assert 0.0 <= u["posterior"] <= 1.0
        assert "prior" in u and "shift" in u
