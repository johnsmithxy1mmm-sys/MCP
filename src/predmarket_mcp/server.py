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
    """Starlette ASGI app for streamable-http, with x402 enforcement middleware.

    Exposed so ``uvicorn`` / ``fastmcp run`` can serve it directly.
    """
    from .billing.middleware import asgi_middleware

    return mcp.http_app(transport="streamable-http", middleware=asgi_middleware())


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
