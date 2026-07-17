"""Retrieval reranking behind a Protocol — Cohere Stub/Real selected by config.

Pinned surface (W2_ARCHITECTURE.md §RAG): ``build_reranker(settings)`` returns
a :class:`Reranker` whose ``rerank(query, documents)`` returns the same
candidate strings reordered most-relevant-first. Mirrors ``build_embedder``: an
empty ``cohere_api_key`` (the default) selects the deterministic keyless Stub —
no network, CI-safe — so callers never branch on "do we have a key?".

The reranker is a *quality* refinement layered on top of the fused sparse+dense
ranking, never a correctness dependency: the retriever treats a reranker
failure or absence as a fallback to the fused order (see ``retriever``). Rerank
input is already de-identified by the retriever's ``deidentify`` choke point,
so neither the stub nor the real Cohere client ever sees patient identifiers.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol

import httpx

from copilot.config import Settings
from copilot.rag._lexical import overlap_score, tokenize
from copilot.resilience import (
    RERANK_RETRY,
    RERANK_TIMEOUT,
    RetryPolicy,
    retry_sync,
    retryable_response,
)


class RerankError(RuntimeError):
    """A real reranker call failed (non-2xx response or malformed body)."""


class Reranker(Protocol):
    """Anything that reorders candidate documents by relevance to a query."""

    def rerank(self, query: str, documents: Sequence[str]) -> list[str]:
        """Return ``documents`` reordered most-relevant-first (a permutation)."""
        ...


class StubReranker:
    """Deterministic, keyless reranker (the no-``cohere_api_key`` path).

    Scores each candidate by lexical overlap with the query and returns them
    highest-first, breaking ties by original position (a stable sort). Fully
    offline and reproducible: identical inputs always yield the identical
    order, and a candidate that plainly matches the query terms sorts ahead of
    ones that do not.
    """

    def rerank(self, query: str, documents: Sequence[str]) -> list[str]:
        docs = list(documents)
        query_tokens = tokenize(query)
        ranked = sorted(
            enumerate(docs),
            key=lambda pair: (-overlap_score(query_tokens, pair[1]), pair[0]),
        )
        return [doc for _index, doc in ranked]


class CohereReranker:
    """Real Cohere reranker (``rerank-v3.5`` over HTTPS).

    Active only when ``cohere_api_key`` is set (never in tests/CI). Sends the
    already de-identified query plus candidate texts to Cohere's ``/v2/rerank``
    endpoint and returns the candidates reordered by the returned relevance
    ranking. Any transport or shape error raises :class:`RerankError`; the
    retriever catches it and falls back to the fused order, so a Cohere outage
    degrades ranking quality without ever failing the answer path.

    **Retried** (:data:`copilot.resilience.RERANK_RETRY`: 2 bounded, jittered
    attempts) on timeouts, connection errors, 429 and 5xx — never on any other
    4xx. A rerank commits nothing upstream, so re-sending is safe. The budget is
    deliberately the smallest in the service: this is the one call with an
    instant, lossless fallback, so a second retry would spend a clinician's
    latency to recover a refinement they will barely notice the absence of.

    **Exhausting the retries changes nothing about the failure.** The final
    attempt's error propagates untouched and still becomes :class:`RerankError`,
    which the retriever still catches to serve the fused sparse+dense order. The
    retry can only convert an outage into a success — never a graceful degrade
    into a raised error.
    """

    _ENDPOINT = "https://api.cohere.com/v2/rerank"

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        timeout: httpx.Timeout | float = RERANK_TIMEOUT,
        retry: RetryPolicy = RERANK_RETRY,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._timeout = timeout
        self._retry = retry

    def rerank(self, query: str, documents: Sequence[str]) -> list[str]:
        docs = list(documents)
        if not docs:
            return []
        payload = {
            "model": self._model,
            "query": query,
            "documents": docs,
            "top_n": len(docs),
        }
        try:
            response = retry_sync(
                lambda: httpx.post(
                    self._ENDPOINT,
                    json=payload,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    timeout=self._timeout,
                ),
                policy=self._retry,
                should_retry_result=retryable_response,
            )
            response.raise_for_status()
            body = response.json()
        except httpx.HTTPError as exc:
            raise RerankError("Cohere rerank request failed") from exc
        return _order_from_results(body, docs)


def _order_from_results(body: Any, documents: list[str]) -> list[str]:
    """Map Cohere's ``results`` (index + relevance_score) back to documents."""
    if not isinstance(body, dict):
        raise RerankError("Cohere rerank response was not a JSON object")
    results = body.get("results")
    if not isinstance(results, list):
        raise RerankError("Cohere rerank response missing a 'results' array")
    ordered: list[str] = []
    for entry in results:
        if not isinstance(entry, dict):
            raise RerankError("Cohere rerank result entry was not an object")
        index = entry.get("index")
        if not isinstance(index, int) or not (0 <= index < len(documents)):
            raise RerankError("Cohere rerank result carried an out-of-range index")
        ordered.append(documents[index])
    return ordered


def build_reranker(settings: Settings) -> Reranker:
    """Select the reranker implementation from the current settings."""
    if not settings.cohere_api_key:
        return StubReranker()
    return CohereReranker(settings.cohere_api_key, settings.cohere_rerank_model)
