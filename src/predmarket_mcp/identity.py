"""Caller identity — derived, never accepted (audit INV-002).

The previous model took the ``x-client-id`` header at face value and used it as
the owner of watches, alerts, portfolios and leaderboard entries. Ownership was
then "checked" by comparing a caller-supplied URI parameter against a
caller-supplied header — two values the attacker controls, so the check was
vacuous and any caller could read any client's data.

The rule here is that identity comes only from something the caller cannot
forge, in descending order of strength:

1. **OAuth subject** — the token's signature was verified by the auth layer
   before the request reached us.
2. **x402 payer address** — the payment carried an EIP-712 signature the
   facilitator verified before settling. "Whoever paid owns it" is the natural
   identity for a paid server, and it costs the caller money to claim.
3. **Ephemeral connection identity** — a hash of the forwarded address and user
   agent. NOT proven: two callers behind one NAT with the same client share it.
   It exists so the free tier works without an account, and it is marked
   ``proven=False`` so callers know their data is not durably theirs.

``x-client-id`` survives only as a *namespace hint inside an already-proven
identity* (one operator running several agents under one token). It can never
select a different principal, and it is ignored entirely for ephemeral callers —
otherwise the caller would be choosing their own identity again.

Personal resources are addressed as ``me``; there is no URI that names another
principal, so this class of IDOR is impossible by construction rather than by
check. ``REQUIRE_PROVEN_IDENTITY=on`` additionally refuses personal resources to
ephemeral callers, for deployments that want that.
"""

from __future__ import annotations

import hashlib
import os
from contextvars import ContextVar
from dataclasses import dataclass

# Set by the x402 ASGI middleware once a payment has been VERIFIED and settled;
# read by the tool/resource layer downstream in the same request context.
_PAYER: ContextVar[str | None] = ContextVar("x402_payer", default=None)


def set_verified_payer(address: str | None) -> None:
    """Record the payer of a verified payment for the rest of this request."""
    _PAYER.set(address)


def _hint(headers: dict) -> str:
    """Optional sub-namespace within a proven identity (never an identity)."""
    raw = (headers.get("x-client-id") or "").strip()
    if not raw:
        return ""
    # Bound it and strip separators so a hint can never forge another principal's
    # full id (e.g. "oauth:victim").
    safe = "".join(ch for ch in raw if ch.isalnum() or ch in "-_")[:64]
    return f"/{safe}" if safe else ""


@dataclass(frozen=True)
class Identity:
    """Who is calling, and whether that claim was proven."""

    id: str
    source: str      # "oauth" | "x402" | "ephemeral"
    proven: bool

    def as_dict(self) -> dict:
        return {"identity": self.id, "identity_source": self.source,
                "identity_proven": self.proven}


def _headers() -> dict:
    try:
        from fastmcp.server.dependencies import get_http_headers

        return get_http_headers(include_all=True) or {}
    except Exception:
        return {}


def caller_identity() -> Identity:
    """The calling principal. Never derived from a value the caller chose."""
    headers = _headers()
    hint = _hint(headers)

    # 1. OAuth subject — proven by signature verification upstream.
    try:
        from fastmcp.server.dependencies import get_access_token

        token = get_access_token()
        if token is not None:
            subject = getattr(token, "subject", None) or getattr(token, "client_id", None)
            if subject:
                return Identity(f"oauth:{subject}{hint}", "oauth", True)
    except Exception:
        pass

    # 2. x402 payer — proven by the signature the facilitator verified.
    payer = _PAYER.get()
    if payer:
        return Identity(f"x402:{str(payer).lower()}{hint}", "x402", True)

    # 3. Ephemeral connection identity. The hint is deliberately NOT applied:
    #    letting an unproven caller pick a sub-namespace re-opens self-chosen
    #    identity, which is the whole defect being fixed.
    ip = (headers.get("x-forwarded-for", "").split(",")[0] or "").strip()
    ua = headers.get("user-agent", "")
    if ip or ua:
        digest = hashlib.sha256(f"{ip}|{ua}".encode()).hexdigest()[:16]
        return Identity(f"anon:{digest}", "ephemeral", False)
    return Identity("anon:local", "ephemeral", False)


def require_proven() -> bool:
    return os.getenv("REQUIRE_PROVEN_IDENTITY", "").lower() in ("1", "on", "true", "yes")


def personal_access_error(identity: Identity) -> dict | None:
    """``None`` when this caller may read its own personal resources."""
    if identity.proven or not require_proven():
        return None
    return {
        "error": "identity_required",
        "detail": "This deployment requires a proven identity (OAuth token or a "
                  "verified x402 payment) for personal resources.",
        **identity.as_dict(),
    }
