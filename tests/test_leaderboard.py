"""K4: agent leaderboard — proof-of-alpha (paper trades, P&L, ranking)."""

from __future__ import annotations

import json

import pytest

from core.leaderboard import PaperTradeStore, handle_for


def _store(tmp_path, min_resolved=1, monkeypatch=None):
    if monkeypatch is not None:
        monkeypatch.setenv("LEADERBOARD_MIN_RESOLVED", str(min_resolved))
    return PaperTradeStore(f"sqlite:///{tmp_path / 'lb.db'}")


def _yes(mid, entry):
    return {"venue": "kalshi", "market_id": mid, "side": "yes", "entry_price": entry}


# --- P&L math ---------------------------------------------------------------
def test_winning_trade_pnl(tmp_path, monkeypatch):
    store = _store(tmp_path, 1, monkeypatch)
    store.record("A", 1000, [_yes("m1", 0.6)], "bet")
    store.resolve({"m1": 1})                      # YES at 0.6 -> edge (1-.6)/.6
    s = store.client_summary("A")
    assert s["resolved_trades"] == 1
    assert s["total_pnl_usd"] == pytest.approx(666.67, abs=0.5)
    assert s["roi"] == pytest.approx(0.6667, abs=1e-3)
    assert s["hit_rate"] == 1.0


def test_losing_trade_pnl(tmp_path, monkeypatch):
    store = _store(tmp_path, 1, monkeypatch)
    store.record("B", 1000, [_yes("m2", 0.5)], "bet")
    store.resolve({"m2": 0})                      # YES resolved NO -> lose the stake
    s = store.client_summary("B")
    assert s["total_pnl_usd"] == pytest.approx(-1000.0, abs=0.5)
    assert s["hit_rate"] == 0.0


def test_multi_leg_basket_pnl(tmp_path, monkeypatch):
    store = _store(tmp_path, 1, monkeypatch)
    # Two legs, one wins one loses: cost 1.1, payoff 1 -> edge (1-1.1)/1.1.
    legs = [_yes("m1", 0.6),
            {"venue": "kalshi", "market_id": "m2", "side": "no", "entry_price": 0.5}]
    store.record("C", 1000, legs, "spread")
    store.resolve({"m1": 1, "m2": 1})             # m1 YES wins, m2 NO loses
    s = store.client_summary("C")
    assert s["total_pnl_usd"] == pytest.approx(1000 * (1 - 1.1) / 1.1, abs=0.5)


def test_pending_until_all_legs_resolve(tmp_path, monkeypatch):
    store = _store(tmp_path, 1, monkeypatch)
    store.record("D", 1000, [_yes("m1", 0.6), _yes("m2", 0.5)], "two")
    store.resolve({"m1": 1})                       # only one leg known -> not graded
    assert store.client_summary("D")["resolved_trades"] == 0
    # Graded once a pass carries every leg's outcome (as the resolver collects them).
    store.resolve({"m1": 1, "m2": 1})
    assert store.client_summary("D")["resolved_trades"] == 1


# --- ranking ----------------------------------------------------------------
def test_ranking_by_pnl(tmp_path, monkeypatch):
    store = _store(tmp_path, 1, monkeypatch)
    store.record("winner", 1000, [_yes("m1", 0.6)], "w"); store.resolve({"m1": 1})
    store.record("loser", 1000, [_yes("m2", 0.5)], "l"); store.resolve({"m2": 0})
    board = store.leaderboard()
    assert [e["rank"] for e in board["ranked"]] == [1, 2]
    assert board["ranked"][0]["handle"] == handle_for("winner")
    assert board["ranked"][0]["total_pnl_usd"] > board["ranked"][1]["total_pnl_usd"]


def test_provisional_below_min_resolved(tmp_path, monkeypatch):
    store = _store(tmp_path, 3, monkeypatch)       # need 3 resolved to rank
    store.record("rookie", 1000, [_yes("m1", 0.6)], "one"); store.resolve({"m1": 1})
    board = store.leaderboard()
    assert board["ranked"] == [] and board["provisional"] == 1
    # The rookie still sees their own stats, just no rank yet.
    assert store.client_summary("rookie")["rank"] is None


def test_handle_is_anonymous_and_stable():
    h = handle_for("auth-deadbeef")
    assert h.startswith("agent-") and "auth-deadbeef" not in h
    assert h == handle_for("auth-deadbeef")          # stable
    assert h != handle_for("auth-cafef00d")          # distinct per client


# --- integrity guards -------------------------------------------------------
def test_size_is_clamped_to_the_cap(tmp_path, monkeypatch):
    # A self-declared paper size is free to claim; without the clamp, "$1B on a
    # coin flip" would top the ranking. The cap bounds every trade's size.
    monkeypatch.setenv("LEADERBOARD_MAX_SIZE_USD", "10000")
    store = _store(tmp_path, 1, monkeypatch)
    store.record("whale", 1_000_000_000, [_yes("m1", 0.5)], "flip")
    store.resolve({"m1": 1})
    s = store.client_summary("whale")
    assert s["deployed_usd"] == 10000.0
    assert s["total_pnl_usd"] == pytest.approx(10000 * 1.0, abs=1.0)  # not 1e9-scale


def test_open_trade_cap_refuses_further_commits(tmp_path, monkeypatch):
    monkeypatch.setenv("LEADERBOARD_MAX_OPEN", "2")
    store = _store(tmp_path, 1, monkeypatch)
    store.record("spammer", 100, [_yes("m1", 0.5)], "a")
    store.record("spammer", 100, [_yes("m2", 0.5)], "b")
    with pytest.raises(RuntimeError):
        store.record("spammer", 100, [_yes("m3", 0.5)], "c")
    # Resolution frees capacity.
    store.resolve({"m1": 1, "m2": 1})
    assert store.record("spammer", 100, [_yes("m3", 0.5)], "c")


def test_commit_tool_degrades_gracefully_at_cap(monkeypatch):
    # The tool path returns a response WITHOUT paper_trade_id instead of erroring.
    monkeypatch.setenv("LEADERBOARD_MAX_OPEN", "0")
    from predmarket_mcp import deps
    from core.models import Leg, Side, Venue

    tid = deps.commit_paper_trade(
        "cid-cap", [Leg(venue=Venue.KALSHI, market_id="kx-btc-100k-eoy26", side=Side.YES)], 100)
    assert tid is None


# --- integration through deps + MCP surface ---------------------------------
def test_resolver_grades_paper_trades():
    from predmarket_mcp import deps
    from core.models import Leg, Side, Venue

    tid = deps.commit_paper_trade(
        "cid-x", [Leg(venue=Venue.KALSHI, market_id="kx-btc-100k-eoy26", side=Side.YES)], 1000)
    assert tid is not None
    deps.resolve_outcomes({"kx-btc-100k-eoy26": 1})   # autonomous resolver path
    assert deps.client_portfolio("cid-x")["resolved_trades"] == 1


@pytest.mark.asyncio
async def test_commit_records_and_portfolio_reads_back(client):
    legs = [{"venue": "kalshi", "market_id": "kx-btc-100k-eoy26", "side": "yes"}]
    r = (await client.call_tool(
        "estimate_execution", {"legs": legs, "size_usd": 1000, "commit": True})).data
    assert "paper_trade_id" in r
    cid = r["leaderboard"].split("portfolio://")[1]
    pf = json.loads((await client.read_resource(f"portfolio://{cid}"))[0].text)
    assert pf["trades"][0]["trade_id"] == r["paper_trade_id"]


@pytest.mark.asyncio
async def test_commit_off_by_default(client):
    legs = [{"venue": "kalshi", "market_id": "kx-btc-100k-eoy26", "side": "yes"}]
    r = (await client.call_tool(
        "estimate_execution", {"legs": legs, "size_usd": 1000})).data
    assert "paper_trade_id" not in r


@pytest.mark.asyncio
async def test_portfolio_resource_rejects_other_clients(client):
    pf = json.loads((await client.read_resource("portfolio://someone-else"))[0].text)
    assert pf["error"] == "forbidden"


@pytest.mark.asyncio
async def test_leaderboard_resource_is_signed(client):
    lb = json.loads((await client.read_resource("leaderboard://top"))[0].text)
    assert "ranked" in lb and "provenance" in lb
