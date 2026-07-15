"""Text embedding backends for the semantic matcher tier.

Pluggable, mirroring the metering/storage pattern. The default backend is
``fastembed`` (ONNX bge-small — small, CPU-only, no torch); credentials/model
name come from the environment. If the package or model weights are unavailable
(e.g. no network to download them), ``build_embedder`` returns ``None`` and the
matcher falls back to the lexical tier — the server never crashes.

  EMBED_BACKEND   fastembed (default) | none
  EMBED_MODEL     BAAI/bge-small-en-v1.5 (default)

Anthropic/Claude has no embeddings endpoint; for a hosted rail, Voyage AI is the
recommended provider (add a VoyageEmbedder here, keyed by VOYAGE_API_KEY).
"""

from __future__ import annotations

import math
import os
from abc import ABC, abstractmethod
from functools import lru_cache


class Embedder(ABC):
    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]:
        """Return one vector per input text."""


class FastEmbedEmbedder(Embedder):
    """Local ONNX embeddings via ``fastembed`` (bge-small by default).

    First use downloads the model weights once (Hugging Face); afterwards it runs
    fully offline. Bake the weights into the image to avoid runtime egress.
    # TODO: pin a local weights path for air-gapped deploys.
    """

    def __init__(self, model_name: str):
        from fastembed import TextEmbedding  # lazy: optional dependency

        self._model = TextEmbedding(model_name)

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [list(map(float, v)) for v in self._model.embed(list(texts))]


class VoyageEmbedder(Embedder):  # pragma: no cover - stub
    """Hosted embeddings via Voyage AI (Anthropic's recommended provider).

    # TODO: implement against api.voyageai.com using VOYAGE_API_KEY. Stubbed so
    # the interface exists without pulling the dependency.
    """

    def __init__(self, model_name: str):
        self._model = model_name

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError("VoyageEmbedder is a stub — set EMBED_BACKEND=fastembed.")


@lru_cache(maxsize=1)
def get_embedder() -> Embedder | None:
    """Build and cache the configured embedder, or None if unavailable."""
    backend = os.getenv("EMBED_BACKEND", "fastembed").lower()
    if backend in ("none", "off", ""):
        return None
    model = os.getenv("EMBED_MODEL", "BAAI/bge-small-en-v1.5")
    try:
        if backend == "voyage":
            return VoyageEmbedder(os.getenv("EMBED_MODEL", "voyage-3.5-lite"))
        return FastEmbedEmbedder(model)
    except Exception:
        # Package missing, or weights can't be fetched (no egress) — degrade to
        # the lexical matcher instead of failing.
        return None


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)
