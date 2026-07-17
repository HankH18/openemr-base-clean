"""Hybrid guideline retriever — sparse + dense, fused with RRF, then reranked.

Pinned surface (W2_ARCHITECTURE.md §RAG):

- ``build_retriever(settings, *, embedder=None, reranker=None)`` builds the
  retriever; the keyword injection points let callers (and the frozen harness)
  substitute doubles. ``None`` selects the keyless stubs from ``settings``.
- ``rrf_fuse(sparse_ids, dense_ids)`` fuses two rankings by Reciprocal Rank
  Fusion: ``score(d) = Σ 1/(k + rank_i(d))``.
- ``GuidelineRetriever.retrieve(query, top_k=N)`` returns typed
  :class:`GuidelineEvidence` — guideline chunks only, each carrying its
  ``chunk_id`` + ``section`` and a :class:`GuidelineCitation`. Never a
  patient-fact ``Claim``; an empty corpus yields ``[]`` (explicit no-evidence,
  no fabricated citation).

Pipeline: the query is routed through the ``deidentify`` choke point *first*,
then :func:`~copilot.rag.query.expand_query` rewrites clinical abbreviations
into the de-identified text (deidentify → expand → retrieve), so every
downstream egress (embedder, reranker) sees only de-identified — and never
raw — text, now enriched for recall. A bounded section-heading boost then
re-prioritises hits whose section matches the query's key terms before the
rerank. Sparse retrieval is Postgres full-text (``to_tsvector``/``plainto_tsquery``)
with a portable term-overlap fallback on SQLite; dense retrieval scores cosine
similarity over the stored embeddings (pgvector on Postgres, a JSON list on
SQLite). The Cohere rerank is a best-effort refinement: its failure or absence
falls back to the fused order and is logged, never raised — the answer path is
never gated on the reranker.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from copilot.config import Settings
from copilot.domain.primitives import CitationSourceType, GuidelineCitation
from copilot.memory.db import session_scope
from copilot.memory.models import GuidelineChunkRow
from copilot.memory.repository import MemoryRepository
from copilot.observability import NoopObservability, Observability, build_observability
from copilot.rag._lexical import overlap_score, tokenize
from copilot.rag.deidentify import deidentify
from copilot.rag.embeddings import Embedder, build_embedder
from copilot.rag.query import expand_query
from copilot.rag.rerank import Reranker, build_reranker

_logger = logging.getLogger(__name__)

#: RRF damping constant. The standard k=60 (Cormack et al.); the acceptance
#: fixtures are rank-invariant in k, so the exact value only affects score
#: magnitudes, never the ordering of clearly-separated candidates.
RRF_K = 60

#: Additive score boost for a chunk whose section heading shares a term with the
#: query. Sized as one top-rank reciprocal term (``1/(k+1)``) — a section match
#: is worth about being one rank higher in one ranking: enough to break near
#: ties in favour of the on-topic section, not enough to bury a strong
#: sparse+dense hit. Applied only to already-retrieved candidates, so it
#: re-prioritises hits and never fabricates retrieval of an unmatched chunk.
SECTION_MATCH_BOOST = 1.0 / (RRF_K + 1)


# --- reciprocal rank fusion -----------------------------------------------------


def rrf_scores(
    sparse_ids: Sequence[str], dense_ids: Sequence[str], *, k: int = RRF_K
) -> dict[str, float]:
    """RRF score per id: ``Σ_i 1/(k + rank_i)`` over the rankings it appears in."""
    scores: dict[str, float] = {}
    for ranking in (sparse_ids, dense_ids):
        for rank, chunk_id in enumerate(ranking, start=1):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (k + rank)
    return scores


def rrf_fuse(
    sparse_ids: Sequence[str], dense_ids: Sequence[str], *, k: int = RRF_K
) -> list[str]:
    """Fuse two rankings into one, highest RRF score first (a de-duplicated union)."""
    scores = rrf_scores(sparse_ids, dense_ids, k=k)
    return [chunk_id for chunk_id, _score in sorted(scores.items(), key=lambda kv: -kv[1])]


# --- typed evidence -------------------------------------------------------------


class GuidelineEvidence(BaseModel):
    """One retrieved guideline chunk, typed as guideline evidence.

    Explicitly *not* a patient-fact :class:`~copilot.domain.contracts.Claim`:
    it carries ``source_type='guideline'`` and a :class:`GuidelineCitation`, so
    a synthesizer can never confuse a guideline recommendation with a grounded
    patient observation. ``chunk_id``/``section`` locate the exact corpus span.
    """

    model_config = ConfigDict(frozen=True)

    source_type: Literal[CitationSourceType.guideline] = CitationSourceType.guideline
    chunk_id: str
    document_id: str
    section: str
    content: str
    score: float
    citation: GuidelineCitation


@dataclass(frozen=True)
class _Candidate:
    """Session-independent snapshot of a chunk row (survives session close)."""

    chunk_id: str
    document_id: str
    section: str
    content: str


# --- retriever ------------------------------------------------------------------


class GuidelineRetriever:
    """Hybrid sparse+dense retriever over the guideline corpus."""

    def __init__(
        self,
        *,
        settings: Settings,
        embedder: Embedder,
        reranker: Reranker,
        observability: Observability | None = None,
    ) -> None:
        self._settings = settings
        self._embedder = embedder
        self._reranker = reranker
        self._is_postgres = settings.database_url.startswith("postgresql")
        self._obs: Observability = observability or NoopObservability()

    async def retrieve(self, query: str, top_k: int = 4) -> list[GuidelineEvidence]:
        """Return the top-``k`` guideline-evidence chunks for ``query``.

        The query is de-identified *then* expanded (deidentify → expand →
        retrieve) before any embedder/reranker call, so egress is both
        PHI-scrubbed and recall-enriched. An empty corpus returns ``[]`` —
        explicit no-evidence, never a fabricated cite.

        Wrapped in the ``guideline.retrieve`` span the OBSERVABILITY.md §7.1
        evidence-retrieval SLO reads its p95 from. The span nests under whatever
        span is already open (the graph's ``evidence-retriever.retrieve``, the
        chat span, …), so it lands inside the correlation-id trace rather than
        as an orphan. Attributes are counts only — never the query text, which
        may carry PHI even before the de-identify choke point.
        """
        async with self._obs.span("guideline.retrieve", top_k=top_k) as span:
            scrubbed = deidentify(query)
            # Abbreviation expansion runs strictly AFTER the PHI choke point, so it
            # only ever sees de-identified text and can never re-introduce PHI.
            expanded = expand_query(scrubbed)
            async with session_scope() as session:
                rows = await MemoryRepository(session).list_guideline_chunks()
                if not rows:
                    # An empty corpus is the deploy-skipped-the-ingest failure the
                    # `guideline_corpus` readiness probe grades; record it on the
                    # span so the trace explains a zero-evidence answer.
                    span.set_attribute("corpus_chunks", 0)
                    span.set_attribute("hits", 0)
                    span.set_output({"hits": 0, "corpus_chunks": 0})
                    return []
                # Dense: embed the (de-identified, expanded) query at retrieve time.
                query_vec = self._embedder.embed([expanded])[0]
                dense_ids = _dense_rank(rows, query_vec)
                sparse_ids = await self._sparse_rank(session, rows, expanded)
                candidates = {
                    str(row.id): _Candidate(
                        chunk_id=str(row.id),
                        document_id=str(row.guideline_document_id),
                        section=row.section or "general",
                        content=row.content,
                    )
                    for row in rows
                }

            scores = _boost_section_matches(rrf_scores(sparse_ids, dense_ids), candidates, expanded)
            fused = [cid for cid, _score in sorted(scores.items(), key=lambda kv: -kv[1])]
            fused_candidates = [candidates[cid] for cid in fused if cid in candidates]
            ordered = self._apply_rerank(expanded, fused_candidates)
            evidence = [_to_evidence(candidate, scores) for candidate in ordered[:top_k]]
            span.set_attribute("corpus_chunks", len(rows))
            span.set_attribute("candidates", len(fused_candidates))
            span.set_attribute("hits", len(evidence))
            span.set_output({"hits": len(evidence), "corpus_chunks": len(rows)})
            return evidence

    async def _sparse_rank(
        self, session: AsyncSession, rows: list[GuidelineChunkRow], query: str
    ) -> list[str]:
        """Postgres full-text ranking with a portable term-overlap fallback."""
        if self._is_postgres:
            try:
                return await _sparse_rank_fulltext(session, query)
            except Exception:  # best-effort FTS; retrieval never gates on it
                _logger.warning(
                    "guideline full-text search failed; using portable term overlap",
                    exc_info=True,
                )
        return _portable_sparse_rank(rows, query)

    def _apply_rerank(self, query: str, candidates: list[_Candidate]) -> list[_Candidate]:
        """Rerank the fused candidates; any failure falls back to the fused order."""
        if not candidates:
            return candidates
        documents = [candidate.content for candidate in candidates]
        try:
            reranked = self._reranker.rerank(query, documents)
        except Exception:  # the contract: fall back to fused order, never error the answer
            _logger.warning(
                "guideline rerank failed; serving fused sparse+dense order",
                exc_info=True,
            )
            return candidates
        return _reorder_to_candidates(reranked, candidates, documents)


def build_retriever(
    settings: Settings,
    *,
    embedder: Embedder | None = None,
    reranker: Reranker | None = None,
    observability: Observability | None = None,
) -> GuidelineRetriever:
    """Build the hybrid retriever; ``None`` injections use the keyless stubs.

    ``observability`` defaults from ``settings`` exactly as the embedder and
    reranker do, so every existing caller (the graph's evidence-retriever
    worker, the chat route) emits the ``guideline.retrieve`` span without an
    edit at the call site. Keyless — no Langfuse creds — resolves to
    ``NoopObservability``, so the span is free and behaviour is unchanged.
    """
    return GuidelineRetriever(
        settings=settings,
        embedder=embedder if embedder is not None else build_embedder(settings),
        reranker=reranker if reranker is not None else build_reranker(settings),
        observability=(
            observability if observability is not None else build_observability(settings)
        ),
    )


# --- ranking helpers ------------------------------------------------------------


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    dot = 0.0
    left_norm = 0.0
    right_norm = 0.0
    for x, y in zip(left, right, strict=False):
        dot += x * y
        left_norm += x * x
        right_norm += y * y
    if left_norm <= 0.0 or right_norm <= 0.0:
        return 0.0
    return dot / (math.sqrt(left_norm) * math.sqrt(right_norm))


def _dense_rank(rows: list[GuidelineChunkRow], query_vec: Sequence[float]) -> list[str]:
    """Rank chunks by cosine similarity to the query vector (highest first).

    Rows arrive ordered by id, and the sort is stable, so equal scores keep a
    deterministic id-ascending order. Chunks with no stored embedding are skipped.
    """
    scored: list[tuple[str, float]] = [
        (str(row.id), _cosine(query_vec, row.embedding))
        for row in rows
        if row.embedding is not None
    ]
    scored.sort(key=lambda pair: -pair[1])
    return [chunk_id for chunk_id, _score in scored]


def _boost_section_matches(
    scores: dict[str, float], candidates: dict[str, _Candidate], query: str
) -> dict[str, float]:
    """Add :data:`SECTION_MATCH_BOOST` to hits whose section matches the query.

    The heading path is a strong topical signal, so a chunk already retrieved by
    sparse/dense ranking whose section shares a key term with the (de-identified,
    expanded) query is promoted by a bounded amount. Only ids already in
    ``scores`` are touched — the boost re-prioritises hits, never resurrects an
    unmatched chunk. Deterministic: a plain dict copy with additive boosts.
    """
    query_terms = set(tokenize(query))
    if not query_terms:
        return scores
    boosted = dict(scores)
    for chunk_id in boosted:
        candidate = candidates.get(chunk_id)
        if candidate is not None and set(tokenize(candidate.section)) & query_terms:
            boosted[chunk_id] += SECTION_MATCH_BOOST
    return boosted


def _portable_sparse_rank(rows: list[GuidelineChunkRow], query: str) -> list[str]:
    """Term-overlap sparse ranking (the SQLite / no-full-text fallback)."""
    query_tokens = tokenize(query)
    scored: list[tuple[str, float]] = [
        (str(row.id), overlap_score(query_tokens, row.content)) for row in rows
    ]
    matches = [(chunk_id, score) for chunk_id, score in scored if score > 0.0]
    matches.sort(key=lambda pair: -pair[1])
    return [chunk_id for chunk_id, _score in matches]


async def _sparse_rank_fulltext(session: AsyncSession, query: str) -> list[str]:
    """Postgres full-text ranking over ``guideline_chunk.content``."""
    stmt = text(
        "SELECT id FROM guideline_chunk "
        "WHERE to_tsvector('english', content) @@ plainto_tsquery('english', :q) "
        "ORDER BY ts_rank(to_tsvector('english', content), plainto_tsquery('english', :q)) "
        "DESC, id"
    )
    result = await session.execute(stmt, {"q": query})
    return [str(row[0]) for row in result.all()]


def _reorder_to_candidates(
    reranked: Sequence[str], candidates: list[_Candidate], documents: list[str]
) -> list[_Candidate]:
    """Map reranked document texts back to candidates, preserving fused order for
    any candidate the reranker dropped."""
    remaining = list(range(len(candidates)))
    order: list[int] = []
    for value in reranked:
        match = next((i for i in remaining if documents[i] == value), None)
        if match is None:
            continue
        order.append(match)
        remaining.remove(match)
    order.extend(remaining)
    return [candidates[i] for i in order]


def _to_evidence(candidate: _Candidate, scores: dict[str, float]) -> GuidelineEvidence:
    citation = GuidelineCitation(
        source_id=candidate.document_id,
        page_or_section=candidate.section,
        field_or_chunk_id=candidate.chunk_id,
        quote_or_value=candidate.content,
    )
    return GuidelineEvidence(
        chunk_id=candidate.chunk_id,
        document_id=candidate.document_id,
        section=candidate.section,
        content=candidate.content,
        score=scores.get(candidate.chunk_id, 0.0),
        citation=citation,
    )
