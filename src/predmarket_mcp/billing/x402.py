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
import hashlib
import json
import os
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


def payment_fingerprint(payment: dict) -> str:
    """Deterministic id of a signed payment, for replay detection.

    Keyed on the signature + authorization (which carries the EIP-3009 nonce),
    so re-presenting the same signed X-PAYMENT yields the same fingerprint.
    """
    payload = payment.get("payload") or {}
    key = json.dumps(
        {"sig": payload.get("signature"), "auth": payload.get("authorization")},
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(key.encode()).hexdigest()


def _sqlite_path_of(db_url: str) -> str:
    if db_url.startswith("sqlite:///"):
        return db_url[len("sqlite:///"):]
    if db_url.startswith("sqlite://"):
        return db_url[len("sqlite://"):]
    return str(Path.cwd() / "metering.db")


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


def _payment_requirements(requirement: PaymentRequirement) -> dict:
    """x402 paymentRequirements object for the facilitator protocol."""
    return {
        "scheme": requirement.scheme,
        "network": requirement.network,
        "maxAmountRequired": str(_to_atomic(requirement.price_usd)),
        "resource": requirement.tool_name,
        "description": f"predmarket-mcp tool '{requirement.tool_name}'",
        "payTo": requirement.pay_to,
        "asset": requirement.currency,
        "maxTimeoutSeconds": 60,
    }


class HttpFacilitator(Facilitator):
    """Real facilitator: verifies + settles a signed x402 payment over HTTP.

    Talks the x402 facilitator protocol (`POST /verify`, `POST /settle`) to a
    Coinbase / self-hosted facilitator that recovers the EIP-712 signature and
    submits the USDC transfer on-chain, returning a settlement tx hash. Point it
    at a facilitator with X402_FACILITATOR_URL. The HTTP client is injectable so
    tests can mock the transport.
    """

    def __init__(self, base_url: str, client_factory=None):
        self.base_url = base_url.rstrip("/")
        self._client_factory = client_factory or self._default_client

    def _default_client(self):
        import httpx

        return httpx.Client(base_url=self.base_url, timeout=20.0)

    def _post(self, path: str, body: dict) -> dict:
        import httpx

        try:
            with self._client_factory() as client:
                resp = client.post(path, json=body)
        except httpx.HTTPError as exc:
            raise PaymentError(f"facilitator {path} failed: {type(exc).__name__}") from exc
        if resp.status_code != 200:
            raise PaymentError(f"facilitator {path} HTTP {resp.status_code}: {resp.text[:120]}")
        try:
            return resp.json()
        except ValueError as exc:
            raise PaymentError(f"facilitator {path} returned non-JSON") from exc

    def verify_and_settle(self, payment: dict, requirement: PaymentRequirement) -> Receipt:
        body = {
            "x402Version": 1,
            "paymentPayload": payment,
            "paymentRequirements": _payment_requirements(requirement),
        }
        verified = self._post("/verify", body)
        if not verified.get("isValid", False):
            raise PaymentError(f"facilitator rejected: {verified.get('invalidReason', 'invalid')}")
        settled = self._post("/settle", body)
        if not settled.get("success", False):
            raise PaymentError(f"settlement failed: {settled.get('errorReason', 'unknown')}")
        tx_hash = settled.get("transaction") or settled.get("txHash")
        auth = (payment.get("payload") or {}).get("authorization") or {}
        return Receipt(
            receipt_id=str(uuid.uuid4()),
            tool_name=requirement.tool_name,
            amount_usd=requirement.price_usd,
            currency=requirement.currency,
            network=requirement.network,
            payer=settled.get("payer") or auth.get("from"),
            pay_to=auth.get("to") or requirement.pay_to,
            tx_hash=tx_hash,
        )


def build_facilitator(settings) -> Facilitator:
    """Real facilitator when X402_FACILITATOR_URL is set; mock otherwise."""
    url = getattr(settings, "x402_facilitator_url", None)
    if url:
        return HttpFacilitator(url)
    return MockFacilitator(settings)


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


# --- replay protection ------------------------------------------------------
_NONCE_PREFIX = "x402:nonce:"


class NonceStore:
    """Records accepted payment fingerprints so a signed X-PAYMENT can't be
    replayed. Without this, one signed header buys unlimited paid calls.

    Uses Redis when available (atomic ``SET NX`` — correct across instances, the
    only safe option behind a load balancer); otherwise a local SQLite table.
    """

    def __init__(self, db_url: str, redis_client=None):
        self._lock = threading.Lock()
        self._path = _sqlite_path_of(db_url)
        self._ttl = int(os.getenv("NONCE_TTL_SECONDS", str(30 * 24 * 3600)))
        self._last_prune: float | None = None  # SQLite TTL prune throttle
        if redis_client is not None:
            self._redis = redis_client
        else:
            try:
                from ..kv import get_redis
                self._redis = get_redis()
            except Exception:
                self._redis = None
        if self._redis is None:
            self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS used_payments "
                "(fingerprint TEXT PRIMARY KEY, ts TEXT NOT NULL)"
            )

    def is_used(self, fingerprint: str) -> bool:
        if self._redis is not None:
            return bool(self._redis.exists(_NONCE_PREFIX + fingerprint))
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM used_payments WHERE fingerprint = ?", (fingerprint,)
            ).fetchone()
            return row is not None

    def mark_used(self, fingerprint: str) -> bool:
        """Return True if newly recorded (accept), False if already seen (replay).

        Redis ``SET NX`` is atomic, so two instances racing the same replayed
        payment can never both accept it.
        """
        if self._redis is not None:
            return bool(self._redis.set(_NONCE_PREFIX + fingerprint, "1", nx=True, ex=self._ttl))
        self._maybe_prune()  # Redis expires keys itself; SQLite needs a sweep
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO used_payments (fingerprint, ts) VALUES (?, ?)",
                (fingerprint, datetime.now(timezone.utc).isoformat()),
            )
            return cur.rowcount > 0

    def _maybe_prune(self) -> None:
        """Delete fingerprints older than the TTL (throttled to hourly). A signed
        payment's EIP-3009 authorization has long expired by then, so dropping
        the row cannot re-open a replay window."""
        import time as _t

        now = _t.monotonic()
        if self._last_prune is not None and now - self._last_prune < 3600:
            return
        self._last_prune = now
        from datetime import timedelta

        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=self._ttl)).isoformat()
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM used_payments WHERE ts < ?", (cutoff,))

    def count(self) -> int:
        if self._redis is not None:
            return sum(1 for _ in self._redis.scan_iter(match=_NONCE_PREFIX + "*"))
        with self._lock, self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM used_payments").fetchone()[0]
