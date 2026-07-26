"""B7: ops — metrics, rate limiting, circuit breaker, structured logging."""

from __future__ import annotations

import json

import pytest

from core.circuit import CircuitBreaker, CircuitOpen
from predmarket_mcp.ops import METRICS, Metrics, RateLimiter, _JsonFormatter


# --- metrics ----------------------------------------------------------------
def test_metrics_counter_and_prometheus():
    m = Metrics()
    m.inc("http_requests_total")
    m.inc("http_requests_total", 2)
    assert m.snapshot()["http_requests_total"] == 3.0
    text = m.render_prometheus()
    assert "predmarket_http_requests_total 3.0" in text


# --- rate limiter -----------------------------------------------------------
def test_rate_limiter_disabled_allows_all():
    rl = RateLimiter(rpm=0)
    assert not rl.enabled
    assert all(rl.allow("c") for _ in range(1000))


def test_rate_limiter_enforces_burst():
    rl = RateLimiter(rpm=60, burst=3)  # bucket of 3
    allowed = [rl.allow("client-a") for _ in range(5)]
    assert allowed[:3] == [True, True, True]
    assert allowed[3] is False and allowed[4] is False
    # A different client has its own bucket.
    assert rl.allow("client-b") is True


# --- circuit breaker --------------------------------------------------------
def test_circuit_opens_after_failures_and_fast_fails():
    cb = CircuitBreaker("venue", fail_max=2, reset_timeout=999)

    def boom():
        raise RuntimeError("down")

    for _ in range(2):
        with pytest.raises(RuntimeError):
            cb.call(boom)
    assert cb.state == "open"
    with pytest.raises(CircuitOpen):
        cb.call(lambda: 1)  # fast-fail while open


def test_circuit_half_open_recovers_on_success():
    cb = CircuitBreaker("venue", fail_max=1, reset_timeout=0)  # immediate half-open

    with pytest.raises(RuntimeError):
        cb.call(lambda: (_ for _ in ()).throw(RuntimeError()))
    assert cb.state == "half_open"       # cooldown 0 -> probe allowed
    assert cb.call(lambda: 42) == 42     # success closes it
    assert cb.state == "closed"


def test_circuit_wraps_adapter_fetch(monkeypatch):
    # A venue returning 503 repeatedly should trip the breaker -> AdapterError.
    import httpx

    from core.adapters import base

    monkeypatch.setenv("CIRCUIT_FAIL_MAX", "2")
    base._CLIENTS.clear()
    from core import circuit
    circuit._BREAKERS.pop("TestVenue", None)  # fresh breaker for this venue name

    def make_client(base_url, timeout=None):
        return httpx.Client(base_url=base_url,
                            transport=httpx.MockTransport(lambda r: httpx.Response(503)))

    monkeypatch.setattr(base, "make_client", make_client)
    for _ in range(2):
        with pytest.raises(base.AdapterError):
            base.fetch_json("https://x", "/y", venue="TestVenue")
    # Now the breaker is open: the next call fast-fails with a circuit message.
    with pytest.raises(base.AdapterError) as ei:
        base.fetch_json("https://x", "/y", venue="TestVenue")
    assert "circuit open" in str(ei.value)


# --- structured logging -----------------------------------------------------
def test_json_formatter_emits_valid_json():
    import logging

    rec = logging.LogRecord("t", logging.INFO, __file__, 1, "hello", None, None)
    parsed = json.loads(_JsonFormatter().format(rec))
    assert parsed["level"] == "INFO" and parsed["msg"] == "hello"


# --- /metrics endpoint ------------------------------------------------------
@pytest.mark.asyncio
async def test_metrics_endpoint():
    import httpx

    from predmarket_mcp.server import build_http_app

    METRICS.inc("http_requests_total")
    app = build_http_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.get("/metrics")
        assert r.status_code == 200
        assert "predmarket_" in r.text


def test_ip_cap_stops_client_id_rotation(monkeypatch):
    # Rotating x-client-id mints fresh per-client buckets; the per-IP cap
    # (rpm x RATE_LIMIT_IP_MULT) still bounds the total from one address.
    monkeypatch.setenv("RATE_LIMIT_IP_MULT", "2")
    rl = RateLimiter(rpm=3, burst=3)
    allowed = 0
    for i in range(20):
        if rl.allow(f"rotated-{i}") and rl.allow_ip("10.0.0.9"):
            allowed += 1
    assert allowed == 6  # 3 rpm x mult 2, not 20


def test_ip_cap_disabled_with_zero_mult(monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_IP_MULT", "0")
    rl = RateLimiter(rpm=1, burst=1)
    assert all(rl.allow_ip("10.0.0.9") for _ in range(50))
