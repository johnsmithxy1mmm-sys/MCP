"""Runtime configuration, sourced from environment variables.

No secrets live in code. Venue API keys, the operator wallet, and billing
provider credentials are read from the environment only.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# Repo root: .../MCP  (config.py is at src/predmarket_mcp/config.py)
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PRICING_PATH = REPO_ROOT / "pricing.yaml"


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    """Immutable settings snapshot for the process."""

    # --- Payment gating --------------------------------------------------
    # Master switch. OFF at launch: the server is *able* to charge, but does
    # not gate paid tools until real usage exists. Flip PAID_ENABLED=true to
    # turn on 402 + metering enforcement.
    paid_enabled: bool = field(default_factory=lambda: _env_bool("PAID_ENABLED", False))

    # Which rail to enforce when paid_enabled is on: "x402" (primary) or
    # "apikey" (metering/OAuth fallback). Metering is always recorded.
    payment_rail: str = field(default_factory=lambda: os.getenv("PAYMENT_RAIL", "x402"))

    # --- Data freshness --------------------------------------------------
    # Free tier serves intentionally delayed data. Paid tools are realtime.
    free_tier_delay_seconds: int = field(
        default_factory=lambda: _env_int("FREE_TIER_DELAY_SECONDS", 60)
    )

    # --- Billing backends (credentials via env only) ---------------------
    pricing_path: Path = field(
        default_factory=lambda: Path(os.getenv("PRICING_PATH", str(DEFAULT_PRICING_PATH)))
    )
    metering_backend: str = field(
        default_factory=lambda: os.getenv("METERING_BACKEND", "local")
    )
    # Local metering store. Default SQLite file => zero external infra.
    metering_db_url: str = field(
        default_factory=lambda: os.getenv(
            "METERING_DB_URL", f"sqlite:///{REPO_ROOT / 'metering.db'}"
        )
    )

    # x402 operator wallet + facilitator (env only; never in code).
    operator_wallet: str | None = field(
        default_factory=lambda: os.getenv("X402_OPERATOR_WALLET")
    )
    x402_facilitator_url: str | None = field(
        default_factory=lambda: os.getenv("X402_FACILITATOR_URL")
    )
    x402_network: str = field(
        default_factory=lambda: os.getenv("X402_NETWORK", "base-sepolia")
    )

    # --- Server ----------------------------------------------------------
    host: str = field(default_factory=lambda: os.getenv("HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: _env_int("PORT", 8000))


def get_settings() -> Settings:
    """Return a fresh settings snapshot read from the current environment."""
    return Settings()
