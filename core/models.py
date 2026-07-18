"""Canonical pydantic models shared by the engine and the MCP layer.

These are the normalized shapes the whole product speaks in. In production the
real engine (`core.matcher`, `core.signals`, `core.edge`, `storage.repo`)
produces and consumes exactly these types; the MCP tools only format them for
agents. Keep this module free of transport/billing concerns.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Venue(str, Enum):
    POLYMARKET = "polymarket"
    KALSHI = "kalshi"
    MANIFOLD = "manifold"


class Side(str, Enum):
    YES = "yes"
    NO = "no"


class OpportunityKind(str, Enum):
    BUNDLE = "bundle"
    CROSS_VENUE = "cross_venue"
    DUTCH_BOOK = "dutch_book"  # mutually-exclusive outcomes priced != 1.0
    ENTAILMENT = "entailment"  # logical-implication / term-structure violation


class Market(BaseModel):
    """A normalized prediction market on a single venue."""

    venue: Venue
    market_id: str
    title: str
    category: str | None = None
    yes_price: float = Field(description="Normalized YES price in [0, 1].")
    no_price: float = Field(description="Normalized NO price in [0, 1].")
    volume_usd: float | None = None
    close_time: datetime | None = None
    # Mutually-exclusive outcome group (e.g. "2028-president-party"); markets that
    # share a group are complementary outcomes whose YES prices should sum to ~1.
    event_group: str | None = None

    @property
    def implied_probability(self) -> float:
        """YES-side implied probability (== yes_price for a normalized book)."""
        return self.yes_price


class OrderbookLevel(BaseModel):
    price: float
    size_usd: float


class OrderbookSnapshot(BaseModel):
    """Top-of-book depth for one market, used by execution estimation."""

    venue: Venue
    market_id: str
    as_of: datetime
    yes_bids: list[OrderbookLevel] = Field(default_factory=list)
    yes_asks: list[OrderbookLevel] = Field(default_factory=list)

    @property
    def top_ask_price(self) -> float | None:
        return self.yes_asks[0].price if self.yes_asks else None

    @property
    def top_bid_price(self) -> float | None:
        return self.yes_bids[0].price if self.yes_bids else None


class Leg(BaseModel):
    """One side of a trade the agent is considering.

    Deliberately flat/minimal to keep the tool schema token-light.
    """

    venue: Venue
    market_id: str
    side: Side


class Opportunity(BaseModel):
    """A detected, realizable mispricing opportunity."""

    kind: OpportunityKind
    title: str
    category: str | None = None
    realizable_edge: float = Field(
        description="Edge AFTER fees, gas, and slippage. Never gross."
    )
    max_size_usd: float = Field(
        description="Dollar size fillable while keeping realizable_edge positive."
    )
    legs: list[Leg]
    detected_at: datetime = Field(default_factory=utcnow)
    # --- risk adjustment (edge is only as good as the time/risk to realize it) ---
    holding_days: float | None = Field(
        default=None,
        description="Days to market resolution — capital is locked until then.",
    )
    annualized_edge: float | None = Field(
        default=None,
        description="realizable_edge annualized over holding_days (edge / days * 365).",
    )
    resolution_risk: float | None = Field(
        default=None,
        description="0..1 haircut for resolution-source / settlement risk.",
    )
    fair_value: float | None = Field(
        default=None,
        description="Liquidity-weighted cross-venue consensus YES probability, "
        "when known — the anchor the edge is measured against.",
    )
    survival_probability: float | None = Field(
        default=None,
        description="Empirical P(this opportunity is still open after the "
        "execution window), learned from how long past opportunities of this "
        "kind/category lasted. None until enough history exists.",
    )
    expected_value: float | None = Field(
        default=None,
        description="realizable_edge weighted by survival_probability — the "
        "honest ranking key ('an edge you can actually reach').",
    )


class MatchedPair(BaseModel):
    """Two markets on different venues judged to be the same real-world event."""

    event: str
    confidence: float = Field(description="Match confidence in [0, 1].")
    a: Market
    b: Market

    @property
    def spread(self) -> float:
        """Absolute YES-price spread between the two venues."""
        return abs(self.a.yes_price - self.b.yes_price)


class ExecutionEstimate(BaseModel):
    """Result of simulating a fill against current depth."""

    requested_size_usd: float
    fillable_size_usd: float
    avg_fill_price: float
    gross_edge: float
    fees_usd: float
    gas_usd: float
    slippage_usd: float
    realizable_edge: float = Field(
        description="Net edge after fees + gas + slippage on the fillable size."
    )
    cost_breakdown: dict = Field(
        default_factory=dict,
        description="Per-venue fee/gas breakdown so the agent can see where cost went.",
    )


class PricePoint(BaseModel):
    """One point in a historical price/spread series."""

    ts: datetime
    yes_price: float
    spread: float | None = None
