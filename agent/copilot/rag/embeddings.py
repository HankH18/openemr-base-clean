"""Dense embeddings behind a Protocol — Voyage Stub/Real selected by config.

Pinned surface (W2_ARCHITECTURE.md §RAG): ``build_embedder(settings)`` returns
an :class:`Embedder` whose ``embed(texts)`` yields one ``EMBEDDING_DIM``-dim
vector per input text. Mirrors ``copilot.agent.factory.build_agent``: an empty
``voyage_api_key`` (the default) selects the deterministic keyless Stub — no
network traffic, CI-safe — so callers never branch on "do we have a key?".

A configured key selects the real Voyage client (``voyage-3.5`` over HTTPS);
the keyless stub stays the CI/test default so no acceptance run ever touches
the network.

**The keyless stub is lexical, not semantic.** :class:`StubEmbedder` is a
hashing-trick bag-of-words (see its docstring): it measures *term overlap*
projected into ``EMBEDDING_DIM`` dimensions. It has no notion of meaning —
"myocardial infarction" and "heart attack" are as unrelated to it as any two
random phrases. Only :class:`VoyageEmbedder` produces semantic vectors. Do not
describe the keyless path as semantic search: keyless deployments run
lexical-hybrid retrieval (two different lexical views fused), not dense-semantic
retrieval.
"""

from __future__ import annotations

import hashlib
import math
from collections import Counter
from collections.abc import Sequence
from typing import Any, Protocol

from copilot.config import Settings
from copilot.memory.db import EMBEDDING_DIM
from copilot.rag._lexical import tokenize


class EmbeddingError(RuntimeError):
    """A real embedding call failed (non-2xx response or malformed body)."""


class Embedder(Protocol):
    """Anything that turns texts into fixed-dimension dense vectors."""

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Return one ``EMBEDDING_DIM``-dim vector per input text, in order."""
        ...


#: Salt prefixing every token before it is hashed to a dimension. Namespaces
#: this feature space and pins it: changing it re-shuffles every vector, so it
#: is part of the on-disk embedding format (see the re-ingest note on
#: :class:`StubEmbedder`). Never change it without a corpus re-ingest.
_FEATURE_SALT = b"copilot.rag.embeddings.StubEmbedder.v2:"


def _hash_feature(token: str, dim: int) -> tuple[int, float]:
    """Map ``token`` to a ``(dimension, sign)`` pair — the signed hashing trick.

    BLAKE2b over the salted token: the low 4 bytes pick the dimension, one
    further bit picks the sign. Signing is what makes the trick unbiased —
    two tokens colliding on a dimension cancel as often as they reinforce, so
    collisions add zero-mean noise instead of a systematic positive bias
    (Weinberger et al. 2009, "Feature Hashing for Large Scale Multitask
    Learning"). Pure ``hashlib`` — no PYTHONHASHSEED dependence, so the mapping
    is byte-identical across processes and machines (Python's built-in ``hash``
    would NOT be).
    """
    digest = hashlib.blake2b(_FEATURE_SALT + token.encode("utf-8"), digest_size=8).digest()
    index = int.from_bytes(digest[:4], "big") % dim
    sign = 1.0 if digest[4] & 1 else -1.0
    return index, sign


class StubEmbedder:
    """Deterministic, keyless, offline **lexical** embedder (no ``voyage_api_key``).

    A hashing-trick bag-of-words — a standard, well-understood vectorizer
    (scikit-learn ships the same thing as ``HashingVectorizer``), not a toy:

    1. Tokenize with :func:`copilot.rag._lexical.tokenize` — the *same*
       tokenizer the sparse leg and the stub reranker use, so all three legs
       agree on what a term is.
    2. Hash each distinct term to one of ``dim`` dimensions with a sign
       (:func:`_hash_feature`) and accumulate its **sublinear term frequency**,
       ``1 + log(count)`` — a repeated term counts for more, but with damped
       returns, so one term repeated twenty times cannot swamp the vector.
    3. **L2-normalize**, which is what makes cosine well-behaved: every vector
       lands on the unit sphere, so cosine reduces to the dot product and
       compares term *composition* rather than document length. Without it a
       long chunk would out-score a short on-topic one purely by having more
       terms.

    Properties this preserves from the keyless contract: fully offline (no
    network, no key), deterministic and byte-stable across processes, runs, and
    machines (``hashlib``, not salted ``hash``), ``dim``-dimensional, fast
    (one pass, no model). A per-instance cache makes repeat embeds free.

    What it gains over the ``sha256(text)``-expansion it replaces: **lexical
    continuity**. SHA-256 is *designed* to destroy input similarity, so hashing
    the whole text made near-identical inputs land at unrelated points —
    ``cos(q, q + " ")`` measured ``-0.04``, and the dense leg fed pure noise
    into the RRF fusion, actively corrupting it. Hashing per *term* instead
    means texts sharing terms share dimensions: ``cos(q, q + " ") == 1.0``, and
    a sepsis query genuinely ranks sepsis chunks above AKI ones.

    **It is lexical, not semantic — this is a real limit, not a hedge.**
    Vectors are term-overlap projections. Two texts saying the same thing in
    different words ("MI" vs. "heart attack") score ~0; the sign of a term is
    lost to the bag (negation is invisible: "start antibiotics" vs. "do not
    start antibiotics" are near-identical). It also cannot weight rare terms —
    IDF needs corpus statistics, and the ``Embedder`` protocol embeds each text
    in isolation. Real semantic retrieval requires a configured
    ``voyage_api_key`` (:class:`VoyageEmbedder`). What this class buys is a
    dense leg that *contributes signal* to RRF instead of corrupting it.

    A text with no tokens (empty, or only stop-words) has no lexical content
    and embeds to the zero vector — cosine ``0.0`` against everything, i.e.
    ranked neutrally rather than given a fabricated position.

    .. warning::
       Vectors are persisted at ingest (``copilot.rag.ingest``). Any change to
       the tokenizer, ``_FEATURE_SALT``, the weighting, or ``dim`` changes the
       feature space, making previously-stored vectors incomparable with newly
       -embedded queries. Re-ingest the corpus from scratch after such a change.
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
        vector = [0.0] * self._dim
        for token, count in Counter(tokenize(text)).items():
            index, sign = _hash_feature(token, self._dim)
            # Sublinear TF: damped so term repetition informs but never dominates.
            vector[index] += sign * (1.0 + math.log(count))
        norm = math.sqrt(sum(value * value for value in vector))
        if norm > 0.0:
            vector = [value / norm for value in vector]
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
