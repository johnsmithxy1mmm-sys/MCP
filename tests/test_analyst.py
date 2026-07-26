"""B5: LLM analyst — offline heuristic, structured parsing, and mock LLM path."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx

from core import analyst
from core.analyst import LLMAnalyst, heuristic_resolution_risk, score_resolution_risk
from core.models import Market, Venue


def _mkt(category="crypto", vol=1_000_000, days=30):
    return Market(
        venue=Venue.KALSHI, market_id="m", title="btc 100k",
        yes_price=0.6, no_price=0.4, category=category, volume_usd=vol,
        close_time=datetime.now(timezone.utc) + timedelta(days=days),
    )


def test_analyst_disabled_by_default(monkeypatch):
    monkeypatch.delenv("ANALYST_ENABLED", raising=False)
    assert analyst.get_analyst() is None


def test_heuristic_orders_risk_sensibly():
    crisp = heuristic_resolution_risk(_mkt("crypto", 1_000_000, 10))
    soft = heuristic_resolution_risk(_mkt("politics", 5_000, 300))
    assert 0.0 <= crisp < soft <= 1.0


def test_score_resolution_risk_falls_back_to_heuristic(monkeypatch):
    monkeypatch.delenv("ANALYST_ENABLED", raising=False)
    out = score_resolution_risk(_mkt())
    assert out["source"] == "heuristic"
    assert 0 <= out["resolution_risk"] <= 1


def test_parse_tool_result_extracts_structured_score():
    payload = {"content": [
        {"type": "thinking", "thinking": "…"},
        {"type": "tool_use", "name": "report_resolution_risk",
         "input": {"resolution_risk": 1.7, "rationale": "ambiguous", "confidence": 0.8}},
    ]}
    parsed = LLMAnalyst.parse_tool_result(payload)
    assert parsed["resolution_risk"] == 1.0   # clamped into [0,1]
    assert parsed["rationale"] == "ambiguous"
    assert parsed["confidence"] == 0.8


def test_parse_tool_result_none_when_absent():
    assert LLMAnalyst.parse_tool_result({"content": [{"type": "text", "text": "hi"}]}) is None


def test_score_uses_mock_llm_then_degrades():
    def ok_factory():
        def handler(req):
            return httpx.Response(200, json={"content": [
                {"type": "tool_use", "name": "report_resolution_risk",
                 "input": {"resolution_risk": 0.15, "rationale": "clear", "confidence": 0.9}},
            ]})
        return httpx.Client(base_url="https://x", transport=httpx.MockTransport(handler))

    a = LLMAnalyst(api_key="k", client_factory=ok_factory)
    out = a.score(_mkt())
    assert out["source"] == "llm" and out["resolution_risk"] == 0.15

    def bad_factory():
        return httpx.Client(base_url="https://x",
                            transport=httpx.MockTransport(lambda r: httpx.Response(500)))

    degraded = LLMAnalyst(api_key="k", client_factory=bad_factory).score(_mkt())
    assert degraded["source"] == "heuristic"  # HTTP 500 -> heuristic, never raises


def test_request_body_has_adaptive_thinking():
    a = LLMAnalyst(api_key="k")
    hard = a._request_body(_mkt("politics", 1_000, 300))
    easy = a._request_body(_mkt("crypto", 1_000_000, 10))
    assert hard["thinking"]["budget_tokens"] > easy["thinking"]["budget_tokens"]
    assert hard["tool_choice"]["name"] == "report_resolution_risk"
