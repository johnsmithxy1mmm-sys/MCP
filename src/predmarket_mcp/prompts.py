"""Reusable workflow prompts — the server's "instructions for use"."""

from __future__ import annotations

from fastmcp import FastMCP

from .billing.tiers import price_str


def register(mcp: FastMCP) -> None:
    @mcp.prompt(
        name="arbitrage_scan_workflow",
        description=(
            "Guided workflow for finding and vetting cross-venue / bundle arbitrage. "
            "Walks the agent from discovery to a ranked, execution-checked shortlist "
            "using this server's tools in the right order."
        ),
    )
    def arbitrage_scan_workflow(min_edge: float = 0.02) -> str:
        return (
            "You are hunting realizable arbitrage across prediction markets "
            "(Polymarket, Kalshi) using the predmarket-mcp tools. Follow these steps "
            "and prefer realizable edge (after fees/gas/slippage) over gross spreads.\n\n"
            f"1. SCAN. Call `find_mispricing(min_edge={min_edge})` to get live "
            f"opportunities above your edge threshold ({min_edge:g}). This is a paid "
            f"tool ({price_str('find_mispricing')}/call) — call it once, not in a loop.\n"
            "2. CONFIRM MATCHES. For cross_venue opportunities, call "
            "`compare_across_venues(event=...)` to confirm the two markets are the "
            "same real-world event and check the current spread and direction.\n"
            "3. CHECK EXECUTION. For each candidate, call `estimate_execution(legs=..., "
            "size_usd=...)` at the size you would actually trade. Keep only opportunities "
            "whose realizable_edge stays positive at that size — depth kills paper edges.\n"
            "4. RANK. Sort survivors by realizable_edge, then by max_size_usd. Note the "
            "`as_of` freshness on each result; discard anything stale.\n"
            "5. REPORT. Return the ranked shortlist with: event, venues, legs, "
            "realizable_edge, fillable size, and match confidence. Do NOT execute trades "
            "— this server provides intelligence only.\n\n"
            "Budget note: steps 1-3 are paid per call. Estimate total cost before running "
            "and avoid re-scanning; reuse results within a single pass."
        )
