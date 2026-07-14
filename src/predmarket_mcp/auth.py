"""OAuth 2.1 wiring for the fallback (API-key / metering) rail.

Streamable HTTP with the API-key rail expects OAuth 2.1 + PKCE. We do NOT write
our own auth — we use FastMCP's built-in verifiers/proxies. This module builds
an ``AuthProvider`` from the environment when configured, and returns ``None``
otherwise (x402 is the primary rail and needs no login).

Config via env only:
  AUTH_JWKS_URI   — JWKS endpoint of your OAuth 2.1 issuer
  AUTH_ISSUER     — expected token issuer
  AUTH_AUDIENCE   — expected audience (this server's resource id)

For a hosted IdP (Auth0/WorkOS/Clerk/Google/...) swap in the matching provider
from ``fastmcp.server.auth.providers`` — same one-line wiring.
"""

from __future__ import annotations

import os

from fastmcp.server.auth import AuthProvider


def build_auth_provider() -> AuthProvider | None:
    """Return a JWT-based OAuth 2.1 verifier if configured, else None."""
    jwks_uri = os.getenv("AUTH_JWKS_URI")
    issuer = os.getenv("AUTH_ISSUER")
    if not jwks_uri or not issuer:
        # Not configured — x402 (primary rail) needs no login. Fallback off.
        return None

    from fastmcp.server.auth.providers.jwt import JWTVerifier

    return JWTVerifier(
        jwks_uri=jwks_uri,
        issuer=issuer,
        audience=os.getenv("AUTH_AUDIENCE"),
    )
