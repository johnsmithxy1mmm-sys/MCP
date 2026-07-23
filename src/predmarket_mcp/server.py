"""FastMCP server instance for predmarket-mcp.

This module is intentionally thin: it constructs the FastMCP app, registers
tools / resources / prompts (defined in their own modules), wires the billing
middleware, and exposes a ``/health`` endpoint. All business logic lives in
``core/`` and is reached via ``deps.py``.
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse

from fastmcp import FastMCP

from . import __version__
from .auth import build_auth_provider
from .config import get_settings

mcp: FastMCP = FastMCP(
    name="predmarket-mcp",
    version=__version__,
    auth=build_auth_provider(),  # OAuth 2.1 fallback rail; None unless configured

    instructions=(
        "Prediction-market intelligence for agents. Normalized data, mispricing "
        "detection, and honest realizable-edge (after fees/gas/slippage) across "
        "Polymarket and Kalshi (optionally Manifold). Free tools (search_markets, "
        "list_venues, evaluate_market, track_record) are for discovery and trust; "
        "paid tools (find_mispricing, compare_across_venues, estimate_execution, "
        "get_market_history, watch, poll_alerts) return realtime intelligence and "
        "are priced per call. Every response marks its data freshness (`as_of` / "
        "`data_age_seconds`); signed provenance is verifiable via GET /pubkey and "
        "the public track record via GET /track-record. This server returns "
        "intelligence only — it never executes trades or holds funds."
    ),
)


@mcp.custom_route("/health", methods=["GET"])
async def health(_request: Request) -> JSONResponse:
    """Liveness probe for load balancers / container orchestration."""
    settings = get_settings()
    return JSONResponse(
        {
            "status": "ok",
            "service": "predmarket-mcp",
            "version": __version__,
            "paid_enabled": settings.paid_enabled,
            "payment_rail": settings.payment_rail if settings.paid_enabled else None,
        }
    )


@mcp.custom_route("/audit/{bundle_id}", methods=["GET"])
async def audit_bundle(request: Request) -> JSONResponse:
    """Public signed evidence bundle for a flagged opportunity — inputs, prices,
    book depth, timestamp + provenance. Independently verifiable against /pubkey."""
    import anyio

    from . import deps

    bid = request.path_params["bundle_id"]
    bundle = await anyio.to_thread.run_sync(deps.get_audit_bundle, bid)
    if bundle is None:
        return JSONResponse({"error": "not_found", "bundle_id": bid}, status_code=404)
    return JSONResponse(bundle)


@mcp.custom_route("/metrics", methods=["GET"])
async def metrics(_request: Request):
    """Prometheus-text metrics (request counts, rate-limits, circuit states)."""
    from starlette.responses import PlainTextResponse

    from .ops import METRICS

    return PlainTextResponse(METRICS.render_prometheus(), media_type="text/plain; version=0.0.4")


@mcp.custom_route("/revenue", methods=["GET"])
async def revenue(request: Request) -> JSONResponse:
    """Operator-only revenue reconciliation (charged vs settled). Requires the
    X-Admin-Token header to match ADMIN_TOKEN; 404 when no token is configured."""
    import hmac
    import os

    admin = os.getenv("ADMIN_TOKEN")
    if not admin:
        return JSONResponse({"error": "revenue endpoint disabled (no ADMIN_TOKEN)"}, status_code=404)
    provided = request.headers.get("x-admin-token", "")
    if not hmac.compare_digest(provided, admin):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    import anyio

    from .billing.middleware import get_billing

    summary = await anyio.to_thread.run_sync(get_billing().revenue_summary)
    return JSONResponse(summary)


@mcp.custom_route("/pubkey", methods=["GET"])
async def pubkey(_request: Request) -> JSONResponse:
    """Public Ed25519 key so anyone can verify signed provenance (Trust v2)."""
    from .provenance import public_key_info

    info = public_key_info()
    if info is None:
        return JSONResponse(
            {"signed": False, "detail": "no Ed25519 signing key configured"}, status_code=404
        )
    return JSONResponse({"signed": True, **info})


@mcp.custom_route("/track-record", methods=["GET"])
async def public_track_record(_request: Request) -> JSONResponse:
    """Public, tamper-evident track record — auditable without an MCP session."""
    import anyio

    from . import deps
    from .provenance import provenance as _prov

    # SQLite reads off the event loop (consistent with the async-IO discipline).
    metrics = await anyio.to_thread.run_sync(deps.track_record_metrics)
    commitment = await anyio.to_thread.run_sync(deps.track_record_commitment)
    anchor = await anyio.to_thread.run_sync(deps.anchor_track_record)  # None unless enabled
    signed = {**metrics, "commitment": commitment}
    body = {"track_record": metrics, "commitment": commitment, "provenance": _prov(signed)}
    if anchor:
        body["onchain_anchor"] = anchor  # public-chain timestamp of the root
    return JSONResponse(body)


# --- Registration -------------------------------------------------------------
# Tools / resources / prompts and billing middleware are registered by importing
# their modules, which call back into `mcp`. Kept as a function so imports are
# explicit and testable.

def _register() -> None:
    from . import tools, resources, prompts  # noqa: F401  (registration side effects)
    from .billing.middleware import install_billing_middleware

    tools.register(mcp)
    resources.register(mcp)
    prompts.register(mcp)
    install_billing_middleware(mcp)


_register()


def _http_middleware():
    """The full HTTP middleware stack — ONE assembly shared by every entrypoint
    (uvicorn app, `python -m`, fastmcp run) so no path silently loses a layer.
    Rate limit outermost (cheap reject before any work), then x402 enforcement.
    """
    from starlette.middleware import Middleware as ASGIMiddleware

    from .billing.middleware import asgi_middleware
    from .ops import RateLimitMiddleware, configure_logging

    configure_logging()
    return [ASGIMiddleware(RateLimitMiddleware), *asgi_middleware()]


def build_http_app():
    """Starlette ASGI app for streamable-http, with ops + x402 middleware.

    Exposed so ``uvicorn`` / ``fastmcp run`` can serve it directly.
    """
    return mcp.http_app(transport="streamable-http", middleware=_http_middleware())


# ASGI entrypoint for `uvicorn predmarket_mcp.server:app`.
app = build_http_app()


if __name__ == "__main__":
    settings = get_settings()
    mcp.run(
        transport="streamable-http",
        host=settings.host,
        port=settings.port,
        middleware=_http_middleware(),
    )
