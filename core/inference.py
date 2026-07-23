"""Bayesian inference chains over the dependency graph (J3).

H2 gives pairwise P(A|B). J3 combines *multiple* pieces of evidence about the
same target — P(A | B, C, …) — by accumulating each piece's weight of evidence in
log-odds (naive Bayes). Start from the market's own price as the prior, then for
every conditioning event update the belief; mutually-exclusive evidence drives
the posterior to ~0, corroborating evidence compounds it. This turns the
pairwise dependency graph into a small belief-update engine agents can reason
with ("given the Fed cut AND inflation fell, what's P(recession)?").

Assumes the evidence is conditionally independent given the target (the naive
Bayes assumption) — flagged in the output, since strongly-dependent evidence
would otherwise be double-counted. Pure/stdlib.
"""

from __future__ import annotations

import math


def _clamp(p: float, eps: float = 1e-6) -> float:
    return min(1.0 - eps, max(eps, p))


def weight_of_evidence(prior_a: float, cond_a_given_e: float) -> float:
    """log-likelihood-ratio contribution of one observed evidence event.

    log[ P(E|A)/P(E|¬A) ] rewritten via Bayes from the pairwise P(A|E) and the
    prior P(A): a piece of evidence that raises P(A) above the prior adds positive
    weight, one that lowers it adds negative weight.
    """
    pa = _clamp(prior_a)
    c = _clamp(cond_a_given_e)
    return math.log((c * (1.0 - pa)) / (pa * (1.0 - c)))


def posterior(prior_a: float, evidence_conditionals: list[float]) -> float:
    """P(A | all evidence) by accumulating weights of evidence in log-odds."""
    pa = _clamp(prior_a)
    log_odds = math.log(pa / (1.0 - pa))
    for c in evidence_conditionals:
        log_odds += weight_of_evidence(prior_a, c)
    # Guard the sigmoid against overflow at extreme accumulated evidence.
    if log_odds > 700:
        return 1.0
    if log_odds < -700:
        return 0.0
    return round(1.0 / (1.0 + math.exp(-log_odds)), 4)


def infer_target(prior_a: float, evidence: list[dict]) -> dict:
    """Posterior for a target given a list of {conditional, label} evidence."""
    conds = [e["conditional"] for e in evidence]
    post = posterior(prior_a, conds)
    return {
        "prior": round(prior_a, 4),
        "posterior": post,
        "shift": round(post - prior_a, 4),
        "evidence_count": len(conds),
        "note": "Naive-Bayes combination — assumes evidence is conditionally "
                "independent given the target.",
    }
