"""Usage metering.

Every paid tool call records exactly one usage record. The backend is pluggable:
- ``LocalBackend`` writes to SQLite (default, zero external infra) or Postgres.
- ``StripeBackend`` / ``MoesifBackend`` are typed stubs behind the same ABC, so
  forwarding usage to a real billing provider is a config change, not a rewrite.

Credentials (Stripe/Moesif keys, Postgres DSN) come from the environment only.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..config import Settings


@dataclass(frozen=True)
class UsageRecord:
    tool_name: str
    price_usd: float
    currency: str
    status: str  # "ok" | "error"
    latency_ms: float
    client_id: str | None
    paid_via: str | None  # "x402" | "apikey" | None (gate off)
    params: dict = field(default_factory=dict)
    ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class MeteringBackend(ABC):
    """A sink for usage records. Exactly one record per paid call."""

    @abstractmethod
    def record(self, usage: UsageRecord) -> None: ...

    def count(self) -> int:  # optional; used by tests
        raise NotImplementedError


class LocalBackend(MeteringBackend):
    """SQLite (default) or Postgres usage store.

    SQLite keeps tests and the Inspector run infra-free. For Postgres set
    METERING_DB_URL=postgresql://... (async driver wired via the `postgres`
    extra).  # TODO: wire asyncpg path for production Postgres.
    """

    def __init__(self, db_url: str):
        self._lock = threading.Lock()
        if db_url.startswith("sqlite:///"):
            self._path = db_url[len("sqlite:///"):]
        elif db_url.startswith("sqlite://"):
            self._path = db_url[len("sqlite://"):]
        else:
            # Fail LOUDLY. This store holds the money: usage, receipts and the
            # x402 replay guard. Silently falling back to cwd/metering.db put
            # all three on the container's ephemeral filesystem instead of the
            # mounted volume, so a one-character typo in METERING_DB_URL lost
            # revenue records AND re-opened consumed payments on every redeploy
            # — with nothing in the logs (audit INV-010).
            raise ValueError(
                f"unsupported METERING_DB_URL {db_url!r}: expected sqlite:///ABSOLUTE/PATH "
                "(note the three slashes plus a leading slash for an absolute path). "
                "The metering store holds billing and replay-protection data and "
                "must not silently land somewhere unintended."
            )
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    price_usd REAL NOT NULL,
                    currency TEXT NOT NULL,
                    status TEXT NOT NULL,
                    latency_ms REAL NOT NULL,
                    client_id TEXT,
                    paid_via TEXT,
                    params TEXT
                )
                """
            )

    def record(self, usage: UsageRecord) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO usage
                    (ts, tool_name, price_usd, currency, status, latency_ms,
                     client_id, paid_via, params)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    usage.ts.isoformat(),
                    usage.tool_name,
                    usage.price_usd,
                    usage.currency,
                    usage.status,
                    usage.latency_ms,
                    usage.client_id,
                    usage.paid_via,
                    json.dumps(usage.params, default=str),
                ),
            )

    def count(self) -> int:
        with self._lock, self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM usage").fetchone()[0]

    def records(self) -> list[dict]:
        with self._lock, self._connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM usage ORDER BY id")]


class StripeBackend(MeteringBackend):  # pragma: no cover - stub
    """Forward usage as Stripe metered-billing events.

    # TODO: implement via stripe.billing.MeterEvent.create(...). Requires
    # STRIPE_API_KEY + a meter id in the environment. Stubbed for this iteration.
    """

    def __init__(self, settings: Settings):
        self._settings = settings

    def record(self, usage: UsageRecord) -> None:
        raise NotImplementedError("StripeBackend is a stub — set METERING_BACKEND=local.")


class MoesifBackend(MeteringBackend):  # pragma: no cover - stub
    """Forward usage to Moesif via the moesifasgi middleware / API.

    # TODO: implement with moesifasgi for ASGI/Starlette or the Moesif API.
    # Requires MOESIF_APPLICATION_ID in the environment. Stubbed for now.
    """

    def __init__(self, settings: Settings):
        self._settings = settings

    def record(self, usage: UsageRecord) -> None:
        raise NotImplementedError("MoesifBackend is a stub — set METERING_BACKEND=local.")


def build_backend(settings: Settings) -> MeteringBackend:
    name = (settings.metering_backend or "local").lower()
    if name == "stripe":
        return StripeBackend(settings)
    if name == "moesif":
        return MoesifBackend(settings)
    return LocalBackend(settings.metering_db_url)
