"""Resolution-criteria diff / settlement basis risk (C2).

The quiet lie under every cross-venue "arbitrage": two markets that read like the
same question can resolve by *different rules* — different data source, different
snapshot time, different behavior if the event is cancelled or ambiguous. When
they do, the "spread" isn't free money, it's an unhedged bet on which rulebook
wins. This module scores that risk and flags when two markets are NOT the same
contract — turning our single biggest false-positive risk into a product.

* Offline heuristic (always available): scores basis risk from venue/category and
  resolution-sensitive wording (snapshot vs cumulative, explicit source, cancel
  clauses). Deterministic, zero deps.
* LLM diff (ANALYST_ENABLED + key): a structured, point-by-point comparison of the
  two resolution framings via the shared analyst client. Degrades to the heuristic.

  ANALYST_ENABLED / ANTHROPIC_API_KEY — enable the LLM diff (see core.analyst)
"""

from __future__ import annotations

import re

from .analyst import get_analyst
from .models import Market

# Wording that changes *how* a market settles — mismatches raise basis risk.
_SNAPSHOT = re.compile(r"\b(close|closing|at end|on \w+ \d|eod|end of day|settlement)\b", re.I)
_CUMULATIVE = re.compile(r"\b(by|before|anytime|ever|reach|hit|at any point)\b", re.I)
_SOURCE = re.compile(r"\b(coinbase|binance|cme|espn|ap|associated press|reuters|official|according to)\b", re.I)

_BASIS_TOOL = {
    "name": "report_basis_risk",
    "description": "Report whether two prediction markets resolve by the same rules.",
    "input_schema": {
        "type": "object",
        "properties": {
            "same_contract": {"type": "boolean", "description": "True only if they settle identically."},
            "basis_risk": {"type": "number", "description": "0..1 risk the two resolve differently."},
            "differences": {"type": "array", "items": {"type": "string"}},
            "confidence": {"type": "number"},
        },
        "required": ["same_contract", "basis_risk", "confidence"],
    },
}


def _framing(title: str) -> str:
    """Coarse resolution framing of a title: snapshot vs cumulative vs unknown."""
    if _CUMULATIVE.search(title):
        return "cumulative"
    if _SNAPSHOT.search(title):
        return "snapshot"
    return "unknown"


def heuristic_basis_risk(a: Market, b: Market) -> dict:
    """Offline basis-risk score for two supposedly-matching markets."""
    factors: list[str] = []
    risk = 0.05  # different venues never resolve from the exact same book
    if a.venue != b.venue:
        factors.append("different venues (independent rulebooks)")

    fa, fb = _framing(a.title), _framing(b.title)
    if fa != fb and "unknown" not in (fa, fb):
        risk += 0.35
        factors.append(f"resolution framing differs: {fa} vs {fb}")

    sa, sb = _SOURCE.search(a.title), _SOURCE.search(b.title)
    if bool(sa) != bool(sb):
        risk += 0.10
        factors.append("one market names an explicit data source, the other doesn't")
    elif sa and sb and sa.group(0).lower() != sb.group(0).lower():
        risk += 0.20
        factors.append(f"different resolution sources: {sa.group(0)} vs {sb.group(0)}")

    if (a.category or "") in ("politics", "world", "other"):
        risk += 0.05
        factors.append("soft category — more settlement ambiguity")

    risk = round(min(1.0, risk), 4)
    return {
        "basis_risk": risk,
        # High risk => treat as NOT the same contract (a spread is an unhedged bet).
        "same_contract": risk < 0.25,
        "differences": factors,
        "confidence": 0.4,
        "source": "heuristic",
    }


def _llm_body(analyst, a: Market, b: Market) -> dict:
    return {
        "model": analyst.model,
        "max_tokens": 3000,
        "thinking": {"type": "enabled", "budget_tokens": 2000},
        "tools": [_BASIS_TOOL],
        "tool_choice": {"type": "tool", "name": "report_basis_risk"},
        "messages": [{
            "role": "user",
            "content": (
                "These two prediction markets look like the same event. Decide "
                "whether they RESOLVE BY THE SAME RULES (source, snapshot time, "
                "cancellation handling). Call report_basis_risk.\n\n"
                f"A) venue={a.venue.value}: {a.title}\n"
                f"B) venue={b.venue.value}: {b.title}"
            ),
        }],
    }


def assess_basis(a: Market, b: Market) -> dict:
    """Best available basis-risk assessment: LLM diff if enabled, else heuristic."""
    analyst = get_analyst()
    if analyst is None:
        return heuristic_basis_risk(a, b)
    raw = analyst.call_tool(_llm_body(analyst, a, b), "report_basis_risk")
    if not raw or "basis_risk" not in raw:
        return heuristic_basis_risk(a, b)
    risk = round(max(0.0, min(1.0, float(raw["basis_risk"]))), 4)
    return {
        "basis_risk": risk,
        "same_contract": bool(raw.get("same_contract", risk < 0.25)),
        "differences": list(raw.get("differences", []))[:6],
        "confidence": round(max(0.0, min(1.0, float(raw.get("confidence", 0.6)))), 4),
        "source": "llm",
    }
