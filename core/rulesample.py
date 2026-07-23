"""Client-sampled rulebook analysis via MCP sampling (K3, the deferred J5).

Basis risk (C2) — do two markets that read like the same question actually resolve
by the same rules? — is the single biggest false-positive in cross-venue
arbitrage, and judging it well needs an LLM to read the fine print. Until now that
meant the OPERATOR paying for a Claude call (ANALYST_ENABLED + key). MCP
*sampling* flips the economics: the server asks the CALLING agent's own model to
do the reading, so the deepest analysis runs at the client's expense and with the
client's model — novel use of a capability almost no MCP server exploits.

This module is transport-agnostic: it builds the sampling prompt and parses the
reply. The actual ``ctx.sample`` round-trip lives in the tool (where the MCP
Context is). Everything degrades: no flag, no client sampling support, or an
unparseable reply → the caller keeps the heuristic/operator-LLM basis assessment.

  RULEBOOK_SAMPLING   unset (default) | on  — ask the client's model to judge basis
"""

from __future__ import annotations

import json
import os

SYSTEM_PROMPT = (
    "You are a prediction-market settlement analyst. Two markets on different "
    "venues look like the same question. Decide whether they RESOLVE BY THE SAME "
    "RULES — same data source, same snapshot vs cumulative timing, same handling of "
    "cancellation/ambiguity. Reply with ONLY a JSON object, no prose, with keys: "
    'same_contract (boolean), basis_risk (number 0..1, higher = more likely to '
    "resolve differently), differences (array of short strings), confidence "
    "(number 0..1)."
)


def sampling_enabled() -> bool:
    return os.getenv("RULEBOOK_SAMPLING", "").lower() in ("1", "on", "true", "yes")


def build_user_message(a, b) -> str:
    """The per-pair prompt describing the two markets to compare."""
    return (
        "Market A:\n"
        f"  venue: {a.venue.value}\n  title: {a.title}\n  category: {a.category}\n\n"
        "Market B:\n"
        f"  venue: {b.venue.value}\n  title: {b.title}\n  category: {b.category}\n\n"
        "Return the JSON verdict."
    )


def parse(text: str | None) -> dict | None:
    """Parse the client's JSON verdict into a normalized basis assessment, or None.

    Tolerant of code fences / surrounding prose: extracts the first JSON object.
    Returns the same shape as ``core.basis.assess_basis`` with source
    ``client_sample``, so callers can drop it in place of the heuristic.
    """
    if not text:
        return None
    raw = text.strip()
    if "{" in raw and "}" in raw:
        raw = raw[raw.index("{"): raw.rindex("}") + 1]
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or "basis_risk" not in data:
        return None
    try:
        risk = round(max(0.0, min(1.0, float(data["basis_risk"]))), 4)
    except (TypeError, ValueError):
        return None
    conf = data.get("confidence", 0.6)
    try:
        conf = round(max(0.0, min(1.0, float(conf))), 4)
    except (TypeError, ValueError):
        conf = 0.6
    differences = data.get("differences", [])
    if not isinstance(differences, list):
        differences = [str(differences)]
    return {
        "basis_risk": risk,
        "same_contract": bool(data.get("same_contract", risk < 0.25)),
        "differences": [str(d) for d in differences][:6],
        "confidence": conf,
        "source": "client_sample",
    }
