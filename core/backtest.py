"""Strategy backtester over recorded price history (C4).

Every other tool answers "what's the edge *now*". This answers "would my rule have
*worked*" — replaying a mean-reversion rule over the server's own recorded price
history with honest costs (the same per-venue fee model and a slippage haircut the
live edge math uses). Two things make it valuable and hard to copy: it runs on the
price history WE accumulated (a competitor starting today has none), and it's the
only way for an agent to validate the rails *before* paying for live scans —
strengthening the trust funnel.

Pure/offline: ``simulate`` operates on a list of ``PricePoint`` so it's fully
testable with synthetic series; the tool layer just feeds it the history store.
"""

from __future__ import annotations

from dataclasses import dataclass

from .algorithms import venue_fee
from .models import PricePoint, Venue


@dataclass(frozen=True)
class BacktestParams:
    entry_edge: float = 0.05   # enter long when price is this far BELOW trailing mean
    exit_edge: float = 0.01    # exit when price recovers to within this of the mean
    window: int = 10           # trailing-mean lookback (points)
    size_usd: float = 1000.0
    slippage_bps: float = 20.0  # 0.20% each fill
    venue: Venue = Venue.POLYMARKET


def simulate(points: list[PricePoint], params: BacktestParams) -> dict:
    """Backtest a mean-reversion rule over a YES-price series.

    Rule: track a trailing mean; go long YES when price dips ``entry_edge`` below
    it (under-priced), exit when it recovers to within ``exit_edge`` of the mean.
    Round-trip P&L is net of venue fees on both fills plus slippage.
    """
    prices = [p.yes_price for p in points]
    n = len(prices)
    if n < params.window + 2:
        return _empty_result("insufficient history for the chosen window")

    slip = params.slippage_bps / 10_000.0
    returns: list[float] = []
    equity = params.size_usd
    peak = equity
    max_dd = 0.0
    in_pos = False
    entry_price = 0.0

    for i in range(params.window, n):
        mean = sum(prices[i - params.window:i]) / params.window
        price = prices[i]
        if not in_pos:
            if mean > 0 and price <= mean * (1 - params.entry_edge):
                in_pos = True
                entry_price = price * (1 + slip)  # pay up on entry
        else:
            if price >= mean * (1 - params.exit_edge) or i == n - 1:
                exit_price = price * (1 - slip)  # give up on exit
                gross = (exit_price - entry_price) / entry_price if entry_price else 0.0
                # Venue fee on both fills (contracts ~ size / price).
                fee = (venue_fee(params.venue, params.size_usd, entry_price)
                       + venue_fee(params.venue, params.size_usd, exit_price))
                # A position can't lose more than 100% — fees on top of a total
                # wipeout must not push net below -1 (negative equity would then
                # flip signs through the compounding below).
                net = max(-1.0, gross - fee / params.size_usd)
                returns.append(round(net, 6))
                equity *= (1 + net)
                peak = max(peak, equity)
                max_dd = max(max_dd, (peak - equity) / peak if peak else 0.0)
                in_pos = False

    return _summarize(returns, equity, params.size_usd, max_dd)


def walk_forward(points: list[PricePoint], params: BacktestParams, split: float = 0.7) -> dict | None:
    """Out-of-sample check: fit-window on the first ``split`` of history, evaluate
    on the rest. Exposes overfitting — a rule that only 'works' in-sample is a
    trap, and no other backtest tool tells you that."""
    n = len(points)
    cut = int(n * split)
    need = params.window + 2
    if cut < need or (n - cut) < need:
        return None
    ins = simulate(points[:cut], params)
    oos = simulate(points[cut:], params)
    ins_ret, oos_ret = ins.get("total_return") or 0.0, oos.get("total_return") or 0.0
    return {
        "in_sample_return": ins_ret,
        "out_of_sample_return": oos_ret,
        "in_sample_trades": ins["trades"],
        "out_of_sample_trades": oos["trades"],
        # Same sign in and out of sample => the edge generalized, not fit to noise.
        "generalizes": (oos_ret > 0) == (ins_ret > 0) and oos["trades"] > 0,
    }


def run(points: list[PricePoint], params: BacktestParams) -> dict:
    """Full backtest: the summary plus an out-of-sample walk-forward check."""
    summary = simulate(points, params)
    wf = walk_forward(points, params)
    if wf is not None:
        summary["walk_forward"] = wf
    return summary


def _empty_result(note: str) -> dict:
    return {
        "trades": 0, "hit_rate": None, "total_return": 0.0,
        "avg_return_per_trade": None, "max_drawdown": 0.0,
        "final_equity_usd": None, "note": note,
    }


def _summarize(returns: list[float], equity: float, start: float, max_dd: float) -> dict:
    if not returns:
        return _empty_result("no trades triggered by the rule over this range")
    wins = sum(1 for r in returns if r > 0)
    mean_r = sum(returns) / len(returns)
    # Sharpe-like: mean/stdev of per-trade returns (unitless, no annualization).
    var = sum((r - mean_r) ** 2 for r in returns) / len(returns)
    stdev = var ** 0.5
    sharpe = round(mean_r / stdev, 4) if stdev > 0 else None
    return {
        "trades": len(returns),
        "hit_rate": round(wins / len(returns), 4),
        "total_return": round((equity - start) / start, 4),
        "avg_return_per_trade": round(mean_r, 6),
        "return_stdev": round(stdev, 6),
        "sharpe_per_trade": sharpe,
        "max_drawdown": round(max_dd, 4),
        "final_equity_usd": round(equity, 2),
        "note": "Net of venue fees + slippage; backtest on recorded history, not a guarantee.",
    }
