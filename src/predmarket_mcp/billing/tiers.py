"""Tool -> tier/price mapping, loaded from ``pricing.yaml``.

Single source of truth for which tools are free vs paid and what each paid call
costs. Both the tool descriptions (copy) and the billing middleware read from
here, so price is never hardcoded in two places.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

from ..config import get_settings


@dataclass(frozen=True)
class ToolPricing:
    tier: str  # "free" | "paid"
    price_usd: float

    @property
    def is_paid(self) -> bool:
        return self.tier == "paid" and self.price_usd > 0


@dataclass(frozen=True)
class Pricing:
    currency: str
    tools: dict[str, ToolPricing]

    def get(self, tool_name: str) -> ToolPricing:
        # Unknown tools default to free — fail open, never charge by accident.
        return self.tools.get(tool_name, ToolPricing(tier="free", price_usd=0.0))

    def is_paid(self, tool_name: str) -> bool:
        return self.get(tool_name).is_paid


def _load(path: Path) -> Pricing:
    data = yaml.safe_load(path.read_text()) if path.exists() else {}
    tools = {
        name: ToolPricing(
            tier=str(spec.get("tier", "free")),
            price_usd=float(spec.get("price_usd", 0) or 0),
        )
        for name, spec in (data.get("tools") or {}).items()
    }
    return Pricing(currency=str(data.get("currency", "USDC")), tools=tools)


@lru_cache(maxsize=1)
def load_pricing() -> Pricing:
    """Load and cache pricing from the configured path."""
    return _load(get_settings().pricing_path)


def price_str(tool_name: str) -> str:
    """Human copy for a tool's price, e.g. '$0.05' or 'free'."""
    p = load_pricing().get(tool_name)
    return "free" if not p.is_paid else f"${p.price_usd:g}"
