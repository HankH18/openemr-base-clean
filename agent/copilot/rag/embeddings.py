"""Dense embeddings behind a Protocol — Voyage Stub/Real selected by config.

Pinned surface (W2_ARCHITECTURE.md §RAG): ``build_embedder(settings)`` returns
an :class:`Embedder` whose ``embed(texts)`` yields one ``EMBEDDING_DIM``-dim
vector per input text. Mirrors ``copilot.agent.factory.build_agent``: an empty
``voyage_api_key`` (the default) selects the deterministic keyless Stub — no
network traffic, CI-safe — so callers never branch on "do we have a key?".

A configured key selects the real Voyage client (``voyage-3.5`` over HTTPS);
the keyless stub stays the CI/test default so no acceptance run ever touches
the network.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from typing import Any, Protocol

from copilot.config import Settings
from copilot.memory.db import EMBEDDING_DIM


class EmbeddingError(RuntimeError):
    """A real embedding call failed (non-2xx response or malformed body)."""


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


class VoyageEmbedder:
    """Real Voyage embedding client (``voyage-3.5`` over HTTPS).

    Active only when ``voyage_api_key`` is set (never in tests/CI). Sends texts
    to Voyage's ``/v1/embeddings`` endpoint and returns one vector per input in
    request order. Any transport or shape error raises :class:`EmbeddingError`
    rather than silently degrading to stub vectors.
    """

    _ENDPOINT = "https://api.voyageai.com/v1/embeddings"

    def __init__(self, api_key: str, model: str, *, timeout: float = 30.0) -> None:
        self._api_key = api_key
        self._model = model
        self._timeout = timeout

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        items = list(texts)
        if not items:
            return []
        import httpx

        try:
            response = httpx.post(
                self._ENDPOINT,
                json={"input": items, "model": self._model},
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=self._timeout,
            )
            response.raise_for_status()
            body = response.json()
        except httpx.HTTPError as exc:
            raise EmbeddingError("Voyage embedding request failed") from exc
        return _vectors_from_response(body, len(items))


def _vectors_from_response(body: Any, expected: int) -> list[list[float]]:
    """Extract embeddings from Voyage's ``data`` array, ordered by ``index``."""
    if not isinstance(body, dict):
        raise EmbeddingError("Voyage response was not a JSON object")
    data = body.get("data")
    if not isinstance(data, list) or len(data) != expected:
        raise EmbeddingError("Voyage response 'data' array was missing or the wrong length")
    ordered: list[list[float]] = [[] for _ in range(expected)]
    for entry in data:
        if not isinstance(entry, dict):
            raise EmbeddingError("Voyage response entry was not an object")
        index = entry.get("index")
        embedding = entry.get("embedding")
        if not isinstance(index, int) or not (0 <= index < expected):
            raise EmbeddingError("Voyage response carried an out-of-range index")
        if not isinstance(embedding, list):
            raise EmbeddingError("Voyage response entry was missing its embedding")
        ordered[index] = [float(value) for value in embedding]
    return ordered


def build_embedder(settings: Settings) -> Embedder:
    """Select the embedder implementation from the current settings."""
    if not settings.voyage_api_key:
        return StubEmbedder()
    return VoyageEmbedder(settings.voyage_api_key, settings.voyage_embedding_model)
