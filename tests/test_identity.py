"""INV-002: идентичность выводится, а не принимается (регрессии аудита)."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from predmarket_mcp.identity import caller_identity, set_verified_payer


def _with_headers(headers):
    return patch("fastmcp.server.dependencies.get_http_headers", return_value=headers)


# --- ядро модели -------------------------------------------------------------
def test_x_client_id_alone_cannot_choose_an_identity():
    """Исходная атака: подставить чужой id заголовком.

    Раньше _client_id() возвращал заголовок дословно, и проверка владения
    сравнивала две подконтрольные атакующему величины.
    """
    with _with_headers({"x-client-id": "victim-corp"}):
        ident = caller_identity()
    assert ident.id != "victim-corp"
    assert "victim-corp" not in ident.id      # даже как подстрока/неймспейс
    assert ident.source == "ephemeral" and ident.proven is False


def test_verified_payer_is_a_proven_identity():
    """Платёж подписан и проверен фасилитатором — плательщик доказан."""
    set_verified_payer("0xABCDEF")
    try:
        with _with_headers({}):
            ident = caller_identity()
        assert ident.source == "x402" and ident.proven is True
        assert ident.id == "x402:0xabcdef"
    finally:
        set_verified_payer(None)


def test_hint_namespaces_only_within_a_proven_identity():
    """x-client-id — подсказка внутри доказанной идентичности, не сама она."""
    set_verified_payer("0xABC")
    try:
        with _with_headers({"x-client-id": "bot-7"}):
            ident = caller_identity()
        assert ident.id == "x402:0xabc/bot-7" and ident.proven is True
        # Подсказка не может подделать чужого принципала.
        with _with_headers({"x-client-id": "../oauth:victim"}):
            other = caller_identity()
        assert other.id.startswith("x402:0xabc/") and "oauth:victim" not in other.id
    finally:
        set_verified_payer(None)


def test_two_ephemeral_callers_are_isolated():
    with _with_headers({"x-forwarded-for": "1.1.1.1", "user-agent": "A"}):
        a = caller_identity()
    with _with_headers({"x-forwarded-for": "2.2.2.2", "user-agent": "A"}):
        b = caller_identity()
    assert a.id != b.id and not a.proven and not b.proven


def test_proven_identity_can_be_required(monkeypatch):
    from predmarket_mcp.identity import personal_access_error

    with _with_headers({}):
        ident = caller_identity()
    assert personal_access_error(ident) is None            # по умолчанию разрешено
    monkeypatch.setenv("REQUIRE_PROVEN_IDENTITY", "on")
    denied = personal_access_error(ident)
    assert denied is not None and denied["error"] == "identity_required"


# --- сквозная проверка исходного эксплойта -----------------------------------
@pytest.mark.asyncio
async def test_header_spoofing_cannot_read_another_portfolio(client):
    """Полный сценарий из docs/audit/repro/repro_idor.py."""
    from core.models import Leg, Side, Venue
    from predmarket_mcp import deps

    deps.create_watch("victim-corp", 0.0)
    deps.commit_paper_trade(
        "victim-corp",
        [Leg(venue=Venue.KALSHI, market_id="kx-btc-100k-eoy26", side=Side.YES)], 5000)

    with _with_headers({"x-client-id": "victim-corp"}):
        pf = json.loads((await client.read_resource("portfolio://me"))[0].text)
        al = json.loads((await client.read_resource("alerts://me"))[0].text)

    assert pf["trades"] == [], "портфель жертвы не должен быть виден"
    assert al["count"] == 0, "алерты жертвы не должны быть видны"
