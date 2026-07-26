# syntax=docker/dockerfile:1
FROM python:3.12-slim

# uv for fast, reproducible installs.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8000

WORKDIR /app

# Optional feature extras baked into the image, e.g.:
#   docker build --build-arg EXTRAS="--extra semantic --extra kalshi --extra postgres"
ARG EXTRAS=""
# When semantic is baked in, pre-download the embedding weights at build time so
# the runtime never needs Hugging Face egress (serverless / air-gapped).
ARG WITH_SEMANTIC=false
ENV FASTEMBED_CACHE_PATH=/app/.fastembed

# Install deps first (better layer caching), then the source.
COPY pyproject.toml uv.lock* README.md ./
COPY src ./src
COPY core ./core
COPY pricing.yaml ./
RUN uv sync --frozen --no-dev $EXTRAS 2>/dev/null || uv sync --no-dev $EXTRAS

# Bake bge-small weights into the image when the semantic tier is enabled.
RUN if [ "$WITH_SEMANTIC" = "true" ]; then \
        uv run python -c "from fastembed import TextEmbedding; TextEmbedding('BAAI/bge-small-en-v1.5')"; \
    fi

EXPOSE 8000

# Streamable HTTP behind a TLS-terminating proxy. /health is the liveness probe.
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health').status==200 else 1)"

# Runs the server with the x402 ASGI middleware wired in (see server.py __main__).
CMD ["uv", "run", "python", "-m", "predmarket_mcp.server"]
