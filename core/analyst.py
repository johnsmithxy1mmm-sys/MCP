"""LLM analyst: resolution-risk scoring for opportunities (B5).

The sharpest, hardest-to-replicate signal in prediction markets isn't price — it's
*resolution risk*: will this market settle the way its wording implies, on time,
from a trustworthy source? A 3% edge on a market with an ambiguous resolution
clause is worth less than a 2% edge on a crisp one. That judgment is exactly what
an LLM is good at, so this module scores it.

OFF by default. When ``ANALYST_ENABLED`` is set AND ``ANTHROPIC_API_KEY`` is
present, :class:`LLMAnalyst` calls Claude with a structured tool so the model
returns a machine-readable ``{resolution_risk, rationale, confidence}`` (no
free-text parsing), with adaptive extended thinking scaled to the market's
complexity. Otherwise :func:`heuristic_resolution_risk` — a fully offline
category/liquidity/horizon heuristic — is used, so the server always has a score.

  ANALYST_ENABLED   unset (default) | on
  ANTHROPIC_API_KEY required for the LLM path
  ANALYST_MODEL     claude-opus-4-8 (default)
  ANALYST_MAX_ENRICH  cap on opportunities enriched per scan (default 3; cost guard)
"""

from __future__ import annotations

import logging
import os

from .models import Market

log = logging.getLogger("predmarket.analyst")

_ANALYST_TOOL = {
    "name": "report_resolution_risk",
    "description": "Report the resolution risk of a prediction market as structured data.",
    "input_schema": {
        "type": "object",
        "properties": {
            "resolution_risk": {
                "type": "number",
                "description": "0..1 risk that the market resolves ambiguously, "
                "late, or against its apparent wording. 0 = crisp/certain.",
            },
            "rationale": {"type": "string", "description": "One or two sentences."},
            "confidence": {"type": "number", "description": "0..1 confidence in the score."},
        },
        "required": ["resolution_risk", "rationale", "confidence"],
    },
}


def analyst_enabled() -> bool:
    return os.getenv("ANALYST_ENABLED", "").lower() in ("1", "on", "true", "yes")


def heuristic_resolution_risk(market: Market) -> float:
    """Offline resolution-risk heuristic in [0, 1] (the always-available baseline).

    Longer-dated, thinly-traded, and 'soft' categories (politics/other) carry more
    settlement ambiguity than near-dated, deep, mechanical markets (crypto price).
    """
    risk = 0.02
    category = (market.category or "").lower()
    if category in ("politics", "world", "other", ""):
        risk += 0.03
    if (market.volume_usd or 0) < 10_000:
        risk += 0.03  # thin markets settle less reliably
    if market.close_time is not None:
        from datetime import datetime, timezone

        days = (market.close_time - datetime.now(timezone.utc)).total_seconds() / 86400.0
        if days > 180:
            risk += 0.03  # distant resolution -> more can go wrong
    return round(min(1.0, risk), 4)


def _thinking_budget(market: Market) -> int:
    """Adaptive extended-thinking budget: harder calls get more room to reason."""
    hard = ((market.category or "").lower() in ("politics", "world", "other")) or (
        (market.volume_usd or 0) < 10_000
    )
    return 4000 if hard else 1024


class LLMAnalyst:
    """Anthropic-backed analyst returning a structured resolution-risk score.

    The HTTP client is built by ``client_factory`` so tests can inject a mock
    transport. Any failure degrades to the heuristic — the analyst never raises.
    """

    def __init__(self, api_key: str | None = None, model: str | None = None, client_factory=None):
        self.api_key = api_key if api_key is not None else os.getenv("ANTHROPIC_API_KEY")
        self.model = model or os.getenv("ANALYST_MODEL", "claude-opus-4-8")
        self.base_url = os.getenv("ANTHROPIC_API_URL", "https://api.anthropic.com")
        self._client_factory = client_factory or self._default_client

    def _default_client(self):
        import httpx

        return httpx.Client(
            base_url=self.base_url,
            timeout=float(os.getenv("ANALYST_TIMEOUT", "30")),
            headers={
                "x-api-key": self.api_key or "",
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
        )

    def _request_body(self, market: Market) -> dict:
        budget = _thinking_budget(market)
        return {
            "model": self.model,
            "max_tokens": budget + 1024,
            "thinking": {"type": "enabled", "budget_tokens": budget},
            "tools": [_ANALYST_TOOL],
            "tool_choice": {"type": "tool", "name": "report_resolution_risk"},
            "messages": [{
                "role": "user",
                "content": (
                    "Assess the RESOLUTION RISK of this prediction market and call "
                    "report_resolution_risk with your structured judgment.\n\n"
                    f"Venue: {market.venue.value}\nTitle: {market.title}\n"
                    f"Category: {market.category}\nYES price: {market.yes_price}\n"
                    f"Volume USD: {market.volume_usd}\nCloses: {market.close_time}"
                ),
            }],
        }

    @staticmethod
    def parse_tool_result(payload: dict) -> dict | None:
        """Extract the tool_use input from a Messages API response, or None."""
        for block in payload.get("content", []):
            if block.get("type") == "tool_use" and block.get("name") == "report_resolution_risk":
                data = block.get("input") or {}
                if "resolution_risk" in data:
                    risk = max(0.0, min(1.0, float(data["resolution_risk"])))
                    return {
                        "resolution_risk": round(risk, 4),
                        "rationale": str(data.get("rationale", "")),
                        "confidence": round(max(0.0, min(1.0, float(data.get("confidence", 0.5)))), 4),
                    }
        return None

    def score(self, market: Market) -> dict:
        """Structured resolution-risk score; degrades to the heuristic on any error."""
        import httpx

        try:
            with self._client_factory() as client:
                resp = client.post("/v1/messages", json=self._request_body(market))
            if resp.status_code != 200:
                raise RuntimeError(f"anthropic HTTP {resp.status_code}")
            parsed = self.parse_tool_result(resp.json())
        except (httpx.HTTPError, RuntimeError, ValueError, KeyError) as exc:
            log.warning("analyst LLM call failed (%s); using heuristic", type(exc).__name__)
            parsed = None
        if parsed is None:
            return {
                "resolution_risk": heuristic_resolution_risk(market),
                "rationale": "heuristic (LLM unavailable)",
                "confidence": 0.4,
                "source": "heuristic",
            }
        parsed["source"] = "llm"
        return parsed


def get_analyst() -> LLMAnalyst | None:
    """The configured analyst, or None when disabled / no API key (use heuristic)."""
    if not analyst_enabled():
        return None
    analyst = LLMAnalyst()
    if not analyst.api_key:
        return None
    return analyst


def score_resolution_risk(market: Market) -> dict:
    """Best available resolution-risk score: LLM if enabled+keyed, else heuristic."""
    analyst = get_analyst()
    if analyst is None:
        return {
            "resolution_risk": heuristic_resolution_risk(market),
            "rationale": "heuristic",
            "confidence": 0.4,
            "source": "heuristic",
        }
    return analyst.score(market)
