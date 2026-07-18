"""Tool behavior tests (mocked core via the mock engine)."""

from __future__ import annotations

import pytest

from predmarket_mcp.config import get_settings


@pytest.mark.asyncio
async def test_catalog_size(client):
    tools = await client.list_tools()
    names = sorted(t.name for t in tools)
    assert names == [
        "assess_portfolio",
        "compare_across_venues",
        "estimate_execution",
        "evaluate_market",
        "find_mispricing",
        "get_market_history",
        "list_venues",
        "poll_alerts",
        "search_markets",
        "simulate_strategy",
        "track_record",
        "watch",
    ]
    # Hard ceiling from the design principles: <= 15 tools (target 10-12).
    assert len(names) <= 15


@pytest.mark.asyncio
async def test_search_markets_shape(client):
    r = (await client.call_tool("search_markets", {"query": "bitcoin"})).data
    assert r["count"] >= 1
    m = r["markets"][0]
    assert {"venue", "market_id", "title", "yes_price", "no_price"} <= set(m)
    assert r["tier"] == "free" and r["price_usd"] == 0
    assert "as_of" in r and "data_age_seconds" in r


@pytest.mark.asyncio
async def test_list_venues_shape(client):
    r = (await client.call_tool("list_venues", {})).data
    venues = {v["venue"] for v in r["venues"]}
    assert {"polymarket", "kalshi"} <= venues


@pytest.mark.asyncio
async def test_evaluate_market_is_delayed_on_free_tier(client):
    from predmarket_mcp import deps
    from datetime import timedelta

    r = (await client.call_tool(
        "evaluate_market", {"venue": "polymarket", "market_id": "pm-btc-100k-2026"}
    )).data
    assert r["realtime"] is False and r["delayed"] is True
    delay = get_settings().free_tier_delay_seconds
    # The price is genuinely old (real history point), not backdated live data.
    assert r["data_age_seconds"] >= delay - 1
    now = deps.now()
    pts = deps.get_history(
        "polymarket", "pm-btc-100k-2026",
        now - timedelta(hours=6), now - timedelta(seconds=delay),
    )
    assert r["market"]["yes_price"] == pts[-1].yes_price  # served from history


@pytest.mark.asyncio
async def test_evaluate_market_not_found(client):
    r = (await client.call_tool(
        "evaluate_market", {"venue": "polymarket", "market_id": "nope"}
    )).data
    assert r["error"] == "market_not_found"


@pytest.mark.asyncio
async def test_find_mispricing_realizable_and_realtime(client):
    r = (await client.call_tool("find_mispricing", {"min_edge": 0.02})).data
    assert r["realtime"] is True
    assert r["tier"] == "paid" and r["price_usd"] > 0
    for o in r["opportunities"]:
        assert o["realizable_edge"] >= 0.02  # threshold respected
        assert o["max_size_usd"] >= 0
        assert o["legs"]


@pytest.mark.asyncio
async def test_find_mispricing_kind_filter(client):
    r = (await client.call_tool(
        "find_mispricing", {"min_edge": 0.0, "kind": "cross_venue"}
    )).data
    assert all(o["kind"] == "cross_venue" for o in r["opportunities"])


@pytest.mark.asyncio
async def test_compare_across_venues(client):
    r = (await client.call_tool(
        "compare_across_venues", {"event": "bitcoin above 100k end of 2026"}
    )).data
    assert r["matched"] is True
    assert 0 <= r["confidence"] <= 1
    assert r["spread"] >= 0
    assert len(r["markets"]) == 2


@pytest.mark.asyncio
async def test_estimate_execution_after_costs(client):
    r = (await client.call_tool(
        "estimate_execution",
        {
            "legs": [{"venue": "polymarket", "market_id": "pm-btc-100k-2026", "side": "yes"}],
            "size_usd": 1000,
        },
    )).data
    est = r["estimate"]
    # Realizable edge is net of fees + gas + slippage.
    assert est["fillable_size_usd"] <= est["requested_size_usd"]
    assert "fees_usd" in est and "gas_usd" in est and "slippage_usd" in est
    assert "realizable_edge" in est


@pytest.mark.asyncio
async def test_get_market_history(client):
    r = (await client.call_tool(
        "get_market_history",
        {
            "venue": "polymarket",
            "market_id": "pm-btc-100k-2026",
            "from_ts": "2026-06-01T00:00:00Z",
            "to_ts": "2026-06-05T00:00:00Z",
        },
    )).data
    assert r["count"] >= 2
    assert r["points"][0]["yes_price"] > 0


@pytest.mark.asyncio
async def test_get_market_history_bad_timestamp(client):
    r = (await client.call_tool(
        "get_market_history",
        {"venue": "polymarket", "market_id": "pm-btc-100k-2026",
         "from_ts": "not-a-date", "to_ts": "2026-06-05T00:00:00Z"},
    )).data
    assert r["error"] == "bad_timestamp"


@pytest.mark.asyncio
async def test_resource_snapshot(client):
    r = await client.read_resource("market://kalshi/kx-btc-100k-eoy26")
    assert r  # content returned


@pytest.mark.asyncio
async def test_prompt_workflow_parametrized(client):
    p = await client.get_prompt("arbitrage_scan_workflow", {"min_edge": 0.05})
    text = p.messages[0].content.text
    assert "0.05" in text
    assert "realizable" in text.lower()
