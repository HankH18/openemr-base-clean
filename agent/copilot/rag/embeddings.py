"""Dense embeddings behind a Protocol — Voyage Stub/Real selected by config.

Pinned surface (W2_ARCHITECTURE.md §RAG): ``build_embedder(settings)`` returns
an :class:`Embedder` whose ``embed(texts)`` yields one ``EMBEDDING_DIM``-dim
vector per input text. Mirrors ``copilot.agent.factory.build_agent``: an empty
``voyage_api_key`` (the default) selects the deterministic keyless Stub — no
network traffic, CI-safe — so callers never branch on "do we have a key?".

The real Voyage client (``voyage-3.5`` over HTTPS) lands with the F6
retriever; until then a configured key fails fast rather than silently
degrading to stub vectors.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from typing import Protocol

from copilot.config import Settings
from copilot.memory.db import EMBEDDING_DIM


class Embedder(Protocol):
    """Anything that turns texts into fixed-dimension dense vectors."""

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Return one ``EMBEDDING_DIM``-dim vector per input text, in order."""
        ...


class StubEmbedder:
    """Deterministic, keyless embedding double (the no-``voyage_api_key`` path).

    Each vector is expanded from ``sha256(text)`` — stable across processes and
    runs (reproducible ingest, byte-identical re-embeds), distinct for distinct
    texts, ``dim`` values in ``[-0.5, 0.5)``. A per-instance cache makes repeat
    embeds of the same text free.
    """

    def __init__(self, dim: int = EMBEDDING_DIM) -> None:
        self._dim = dim
        self._cache: dict[str, list[float]] = {}

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, text: str) -> list[float]:
        cached = self._cache.get(text)
        if cached is not None:
            return list(cached)
        vector: list[float] = []
        seed = hashlib.sha256(text.encode("utf-8")).digest()
        counter = 0
        while len(vector) < self._dim:
            block = hashlib.sha256(seed + counter.to_bytes(4, "big")).digest()
            for offset in range(0, len(block), 4):
                vector.append(int.from_bytes(block[offset : offset + 4], "big") / 2**32 - 0.5)
                if len(vector) == self._dim:
                    break
            counter += 1
        self._cache[text] = vector
        return list(vector)


def build_embedder(settings: Settings) -> Embedder:
    """Select the embedder implementation from the current settings."""
    if not settings.voyage_api_key:
        return StubEmbedder()
    raise NotImplementedError(
        "The real Voyage embedding client is not built yet — it lands with the "
        "F6 retriever. Unset COPILOT_VOYAGE_API_KEY to use the deterministic "
        "keyless stub."
    )
