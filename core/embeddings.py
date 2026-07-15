"""Text embedding backends + a batched cache for the semantic matcher tier.

Pluggable, mirroring the metering/storage pattern:

* ``FastEmbedEmbedder`` — local ONNX bge-small (CPU, no torch). First use
  downloads weights once from Hugging Face; afterwards fully offline.
* ``VoyageEmbedder`` — hosted embeddings via Voyage AI (Anthropic's recommended
  provider; Claude has no embeddings endpoint). No model in the image, so it's
  serverless/cold-start friendly. Keyed by ``VOYAGE_API_KEY``.

If the configured backend is unavailable (package missing, weights unfetchable,
no API key) ``get_embedder`` returns ``None`` and the matcher degrades to the
lexical tier — the server never crashes.

``embed_texts`` caches each string's vector, so the matcher embeds every unique
title once (O(N)) instead of re-embedding per pair (O(N^2)) — this is what makes
a paid hosted embedder practical.

  EMBED_BACKEND   fastembed (default) | voyage | none
  EMBED_MODEL     BAAI/bge-small-en-v1.5 (fastembed) | voyage-3.5-lite (voyage)
  VOYAGE_API_KEY  required for the voyage backend
"""

from __future__ import annotations

import math
import os
from abc import ABC, abstractmethod
from functools import lru_cache

VOYAGE_URL = os.getenv("VOYAGE_API_URL", "https://api.voyageai.com")


class EmbedderError(RuntimeError):
    """Raised when an embedding call fails."""


class Embedder(ABC):
    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]:
        """Return one vector per input text, in input order."""


class FastEmbedEmbedder(Embedder):
    """Local ONNX embeddings via ``fastembed`` (bge-small by default)."""

    def __init__(self, model_name: str):
        from fastembed import TextEmbedding  # lazy: optional dependency

        self._model = TextEmbedding(model_name)

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [list(map(float, v)) for v in self._model.embed(list(texts))]


class VoyageEmbedder(Embedder):
    """Hosted embeddings via Voyage AI (``POST /v1/embeddings``).

    The HTTP client is built by ``client_factory`` so tests can inject a mock
    transport. Any failure maps to ``EmbedderError`` so callers degrade cleanly.
    """

    def __init__(self, model_name: str, api_key: str | None = None, client_factory=None):
        self.model = model_name
        self.api_key = api_key if api_key is not None else os.getenv("VOYAGE_API_KEY")
        self._client_factory = client_factory or self._default_client

    def _default_client(self):
        import httpx

        return httpx.Client(
            base_url=VOYAGE_URL,
            timeout=float(os.getenv("HTTP_TIMEOUT", "12")),
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
        )

    def embed(self, texts: list[str]) -> list[list[float]]:
        import httpx

        try:
            with self._client_factory() as client:
                resp = client.post(
                    "/v1/embeddings",
                    json={"input": list(texts), "model": self.model},
                )
        except httpx.HTTPError as exc:  # connect/proxy/timeout
            raise EmbedderError(f"voyage request failed: {type(exc).__name__}") from exc
        if resp.status_code != 200:
            raise EmbedderError(f"voyage HTTP {resp.status_code}: {resp.text[:120]}")
        try:
            rows = resp.json()["data"]
        except (ValueError, KeyError) as exc:
            raise EmbedderError("voyage returned an unexpected payload") from exc
        # Preserve input order (the API echoes an `index` per row).
        rows = sorted(rows, key=lambda r: r.get("index", 0))
        return [list(map(float, r["embedding"])) for r in rows]


@lru_cache(maxsize=1)
def get_embedder() -> Embedder | None:
    """Build and cache the configured embedder, or None if unavailable."""
    backend = os.getenv("EMBED_BACKEND", "fastembed").lower()
    if backend in ("none", "off", ""):
        return None
    try:
        if backend == "voyage":
            model = os.getenv("EMBED_MODEL", "voyage-3.5-lite")
            embedder = VoyageEmbedder(model)
            if not embedder.api_key:
                return None  # no key -> fall back to lexical
            return embedder
        model = os.getenv("EMBED_MODEL", "BAAI/bge-small-en-v1.5")
        return FastEmbedEmbedder(model)
    except Exception:
        # Package missing, or weights can't be fetched (no egress) — degrade.
        return None


# --- batched vector cache ---------------------------------------------------
_CACHE: dict[str, list[float]] = {}


def embed_texts(texts: list[str]) -> list[list[float]] | None:
    """Return vectors for ``texts``, embedding only cache misses (one batch).

    Returns None if no embedder is available. Caching makes matching O(N) in
    embed calls instead of O(N^2) — critical for paid/hosted backends.
    """
    embedder = get_embedder()
    if embedder is None:
        return None
    missing = [t for t in texts if t not in _CACHE]
    if missing:
        vectors = embedder.embed(missing)
        for text, vec in zip(missing, vectors):
            _CACHE[text] = vec
    return [_CACHE[t] for t in texts]


def clear_cache() -> None:
    """Drop cached vectors (used by tests; also call after a model swap)."""
    _CACHE.clear()


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)
