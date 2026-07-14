"""x402 payment rail (primary, agent-native).

Flow:  request a paid tool -> 402 Payment Required with a signed-payment
challenge -> agent retries with an ``X-PAYMENT`` header (base64 JSON) ->
middleware verifies it via a ``Facilitator`` and lets the call through ->
a receipt is logged for reconciliation.

Real on-chain USDC settlement is out of scope for this iteration: the default
``MockFacilitator`` verifies a well-formed signed payment header and mints a
receipt, with settlement stubbed. The 402 flow, verification, gating and
receipt log are real.  # TODO: real facilitator verify + settlement.

Secrets (operator wallet, facilitator URL) come from the environment only.
"""

from __future__ import annotations

import base64
import json
import sqlite3
import threading
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..config import Settings

USDC_DECIMALS = 6


def _to_atomic(usd: float) -> int:
    return int(round(usd * (10 ** USDC_DECIMALS)))


# --- data types -------------------------------------------------------------
@dataclass(frozen=True)
class PaymentRequirement:
    """The x402 challenge attached to a 402 response for a paid tool."""

    tool_name: str
    price_usd: float
    currency: str
    network: str
    pay_to: str | None
    scheme: str = "exact"

    def to_challenge(self) -> dict:
        return {
            "x402_version": 1,
            "error": "payment_required",
            "accepts": [
                {
                    "scheme": self.scheme,
                    "network": self.network,
                    "asset": self.currency,
                    "amount_atomic": str(_to_atomic(self.price_usd)),
                    "amount_usd": self.price_usd,
                    "pay_to": self.pay_to,
                    "resource": self.tool_name,
                }
            ],
            "message": (
                f"Tool '{self.tool_name}' costs ${self.price_usd:g} {self.currency}. "
                "Retry with an X-PAYMENT header (base64 JSON) authorizing this amount."
            ),
        }


@dataclass(frozen=True)
class Receipt:
    receipt_id: str
    tool_name: str
    amount_usd: float
    currency: str
    network: str
    payer: str | None
    pay_to: str | None
    tx_hash: str | None
    ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class PaymentError(Exception):
    """Raised when an X-PAYMENT header is present but invalid."""


# --- facilitator ------------------------------------------------------------
class Facilitator(ABC):
    """Verifies a signed payment header and settles it."""

    @abstractmethod
    def verify_and_settle(
        self, payment: dict, requirement: PaymentRequirement
    ) -> Receipt: ...


class MockFacilitator(Facilitator):
    """Structural verification + stubbed settlement.

    Verifies scheme/network/recipient/amount and the presence of a signature.
    Does NOT perform real signature recovery or on-chain settlement.
    # TODO: replace with a real facilitator (Coinbase x402 / self-hosted).
    """

    def __init__(self, settings: Settings):
        self._settings = settings

    def verify_and_settle(
        self, payment: dict, requirement: PaymentRequirement
    ) -> Receipt:
        scheme = payment.get("scheme")
        network = payment.get("network")
        payload = payment.get("payload") or {}
        auth = payload.get("authorization") or {}
        signature = payload.get("signature")

        if scheme != requirement.scheme:
            raise PaymentError(f"unsupported scheme: {scheme!r}")
        if network != requirement.network:
            raise PaymentError(f"network mismatch: {network!r} != {requirement.network!r}")
        if not signature:
            raise PaymentError("missing signature")

        try:
            value = int(auth.get("value", 0))
        except (TypeError, ValueError):
            raise PaymentError("invalid authorization value")
        if value < _to_atomic(requirement.price_usd):
            raise PaymentError("insufficient payment amount")

        pay_to = auth.get("to")
        if requirement.pay_to and pay_to and pay_to.lower() != requirement.pay_to.lower():
            raise PaymentError("wrong recipient")

        # Settlement is stubbed: mint a deterministic-ish receipt id + fake tx.
        return Receipt(
            receipt_id=str(uuid.uuid4()),
            tool_name=requirement.tool_name,
            amount_usd=requirement.price_usd,
            currency=requirement.currency,
            network=requirement.network,
            payer=auth.get("from"),
            pay_to=pay_to or requirement.pay_to,
            tx_hash="0xstub-" + uuid.uuid4().hex,  # TODO: real settlement tx hash
        )


def decode_payment_header(header_value: str) -> dict:
    """Decode an X-PAYMENT header (base64-encoded JSON) into a dict."""
    try:
        raw = base64.b64decode(header_value, validate=True)
        return json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        raise PaymentError(f"malformed X-PAYMENT header: {exc}") from exc


def encode_payment_header(payment: dict) -> str:
    """Encode a payment dict as a base64 X-PAYMENT header (helper for clients/tests)."""
    return base64.b64encode(json.dumps(payment).encode()).decode()


# --- receipt store ----------------------------------------------------------
class ReceiptStore:
    """Persists accepted payments for reconciliation (SQLite)."""

    def __init__(self, db_url: str):
        self._lock = threading.Lock()
        if db_url.startswith("sqlite:///"):
            self._path = db_url[len("sqlite:///"):]
        elif db_url.startswith("sqlite://"):
            self._path = db_url[len("sqlite://"):]
        else:
            self._path = str(Path.cwd() / "metering.db")
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS receipts (
                    receipt_id TEXT PRIMARY KEY,
                    ts TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    amount_usd REAL NOT NULL,
                    currency TEXT NOT NULL,
                    network TEXT NOT NULL,
                    payer TEXT,
                    pay_to TEXT,
                    tx_hash TEXT
                )
                """
            )

    def save(self, receipt: Receipt) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO receipts
                    (receipt_id, ts, tool_name, amount_usd, currency, network,
                     payer, pay_to, tx_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    receipt.receipt_id,
                    receipt.ts.isoformat(),
                    receipt.tool_name,
                    receipt.amount_usd,
                    receipt.currency,
                    receipt.network,
                    receipt.payer,
                    receipt.pay_to,
                    receipt.tx_hash,
                ),
            )

    def count(self) -> int:
        with self._lock, self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0]

    def all(self) -> list[dict]:
        with self._lock, self._connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM receipts ORDER BY ts")]
