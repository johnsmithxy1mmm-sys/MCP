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


def _history_price(base: float, args: dict) -> float:
    """get_market_history is a data product — scale with the requested range.

    +$0.005 per 30 days of history, capped at $0.10, floored at the base price.
    A wider query is worth more; a bad/one-day range still pays the base.
    """
    from datetime import datetime

    try:
        def _p(v):
            return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        days = max(0.0, (_p(args["to_ts"]) - _p(args["from_ts"])).total_seconds() / 86400.0)
    except Exception:
        return base
    return min(0.10, base + 0.005 * (days / 30.0))


# Per-tool value-based pricing rules: (base_price, arguments) -> price. Tools not
# listed here charge their flat pricing.yaml price. Every rule must be monotone
# and capped so a caller can always bound cost from the args before paying.
_DYNAMIC_RULES = {"get_market_history": _history_price}


@dataclass(frozen=True)
class Pricing:
    currency: str
    tools: dict[str, ToolPricing]

    def get(self, tool_name: str) -> ToolPricing:
        # Unknown tools default to free — fail open, never charge by accident.
        return self.tools.get(tool_name, ToolPricing(tier="free", price_usd=0.0))

    def is_paid(self, tool_name: str) -> bool:
        return self.get(tool_name).is_paid

    def price_for(self, tool_name: str, arguments: dict | None = None) -> float:
        """Actual charge for a call: the flat price, or a value-based amount when
        the tool has a dynamic rule and arguments are provided."""
        base = self.get(tool_name).price_usd
        rule = _DYNAMIC_RULES.get(tool_name)
        if rule is not None and arguments:
            return round(max(base, rule(base, arguments)), 6)
        return round(base, 6)


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
