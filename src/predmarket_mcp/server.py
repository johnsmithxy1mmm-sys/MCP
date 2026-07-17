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
        "Polymarket and Kalshi. Free tools (search_markets, list_venues, "
        "evaluate_market) are for discovery; paid tools (find_mispricing, "
        "compare_across_venues, estimate_execution, get_market_history) return "
        "realtime intelligence and are priced per call. Every response marks its "
        "data freshness (`as_of` / `data_age_seconds`). This server returns "
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


@mcp.custom_route("/metrics", methods=["GET"])
async def metrics(_request: Request):
    """Prometheus-text metrics (request counts, rate-limits, circuit states)."""
    from starlette.responses import PlainTextResponse

    from .ops import METRICS

    return PlainTextResponse(METRICS.render_prometheus(), media_type="text/plain; version=0.0.4")


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
    from . import deps
    from .provenance import provenance as _prov

    metrics = deps.track_record_metrics()
    commitment = deps.track_record_commitment()
    signed = {**metrics, "commitment": commitment}
    return JSONResponse(
        {"track_record": metrics, "commitment": commitment, "provenance": _prov(signed)}
    )


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


def build_http_app():
    """Starlette ASGI app for streamable-http, with ops + x402 middleware.

    Exposed so ``uvicorn`` / ``fastmcp run`` can serve it directly.
    """
    from starlette.middleware import Middleware as ASGIMiddleware

    from .billing.middleware import asgi_middleware
    from .ops import RateLimitMiddleware, configure_logging

    configure_logging()
    # Rate limit outermost (cheap reject before any work), then x402 enforcement.
    middleware = [ASGIMiddleware(RateLimitMiddleware), *asgi_middleware()]
    return mcp.http_app(transport="streamable-http", middleware=middleware)


# ASGI entrypoint for `uvicorn predmarket_mcp.server:app`.
app = build_http_app()


if __name__ == "__main__":
    settings = get_settings()
    from .billing.middleware import asgi_middleware

    mcp.run(
        transport="streamable-http",
        host=settings.host,
        port=settings.port,
        middleware=asgi_middleware(),
    )
