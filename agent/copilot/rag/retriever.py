"""Hybrid guideline retriever — sparse + dense, fused with RRF, then reranked.

Pinned surface (W2_ARCHITECTURE.md §RAG):

- ``build_retriever(settings, *, embedder=None, reranker=None)`` builds the
  retriever; the keyword injection points let callers (and the frozen harness)
  substitute doubles. ``None`` selects the default for that leg from
  ``settings`` — the keyless stub embedder, and (see below) *no rerank stage*
  at all when no ``cohere_api_key`` is configured.
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
rerank. Sparse retrieval is BM25 (:func:`~copilot.rag._lexical.bm25_scores`),
computed in-process over the retrieved rows on **both** backends; dense
retrieval scores cosine similarity over the stored embeddings (pgvector on
Postgres, a JSON list on SQLite). The Cohere rerank is a best-effort
refinement: its failure or absence falls back to the fused order and is logged,
never raised — the answer path is never gated on the reranker.

**Why the sparse leg is not Postgres full-text.** It was, and that leg was
dead in production. ``plainto_tsquery`` ANDs every term, so a chunk had to
contain *all* of them; measured against the real corpus in the deploy's own
``pgvector/pgvector:pg16``, the FTS leg returned **zero rows for every
realistic clinical question** ("How do I reverse warfarin…" → ``'revers' &
'warfarin' & 'major' & 'life-threaten' & …``, and no chunk says "reverse").
Retrieval silently degraded to dense-only — the shipped "hybrid sparse+dense"
was, keyless and on Postgres, a hashing-trick stub embedder alone. Worse, the
defect was structurally invisible: every test and eval runs SQLite, which took
the *other* branch, so the suite graded a ranker production never executed.
OR-ing the tsquery does not rescue it — ``ts_rank`` uses no corpus-global
statistics (no IDF, by Postgres's own documentation), and measured that way it
ranked the wrong section first on 2 of 6 probes, i.e. it would have *imported*
the IDF defect into production. BM25 in-process fixes both: it is the ranker
``ts_rank`` and term-overlap were each approximating badly, and one path means
the tests grade what ships. It costs nothing — ``retrieve`` already loads every
row and ``_dense_rank`` already scores every row in Python, so the SQL leg
bought no scan it was not already paying for.

.. note::
   ``W2_ARCHITECTURE.md`` §RAG still describes the sparse leg as "Postgres
   FTS" and migration ``0006`` still creates a GIN index on
   ``to_tsvector('english', content)``. Both are now inert and want updating;
   the index is harmless but no longer read.

**The rerank stage exists only when a real, keyed reranker is configured.**
``build_reranker`` answers "stub or Cohere?" and, keyless, hands back
:class:`~copilot.rag.rerank.StubReranker` — a *lexical* double whose whole job
is to keep the keyless path offline and deterministic. It was never a ranker
worth deferring to: it sorts by :func:`~copilot.rag._lexical.overlap_score`, a
raw term-frequency sum with no IDF, no stemming, and no length normalization —
exactly the long-chunk bias :class:`~copilot.rag.embeddings.StubEmbedder` L2-
normalizes *away*. Running it last re-introduced that bias as the final word.
Measured over the shipped ``corpus/`` (19 chunks, keyless): it changed top-1 on
2 of 7 clinical queries and was **wrong both times** — 0 wins, 2 losses. On
"MAP target for septic shock vasopressors" the fused ranking put
``vasopressors-and-map-target`` first (0.03125 sparse+dense **+ the section
boost**) and the stub replaced it with ``recognition-and-screening``, i.e. it
discarded precisely the signal :data:`SECTION_MATCH_BOOST` exists to add.

Of those 2 losses, 1 is attributable to section blindness alone: once
:func:`_rerank_document` feeds the heading, the stub's ``overlap_score`` picks
up "vasopressors"/"map"/"target" and it agrees with the boost on that query.
Re-measured *with* the section fed and the window bounded, the stub still
changes top-1 on 1 of 7 and is still wrong ("What fluids for sepsis
resuscitation?" → ``recognition-and-screening``, over the fused
``initial-resuscitation``): 0 wins, 1 loss. Never better, sometimes worse — so
it is gated out rather than merely fed better input. Keyless, ``fused`` *is*
the served order. See :func:`_default_reranker` for how that is expressed (and
why not an isinstance check).

Retrieval also *retrieves* before it reranks. ``_dense_rank`` scores every row
carrying an embedding, so the fused union is the whole corpus; handing a whole
corpus to a reranker makes the reranker the sole ranker rather than a
refinement of one. The fused list is therefore cut to a bounded candidate
window (:data:`RERANK_WINDOW_MULTIPLIER` times ``top_k``) before the rerank stage.

Every remote leg is best-effort in exactly the same sense: an embedder failure
degrades the hybrid to sparse-only (RRF over an empty dense ranking *is* the
sparse ranking) and is logged and marked on the span, never raised. The answer
path is gated on the corpus, not on any network call.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict

from copilot.config import Settings
from copilot.domain.primitives import CitationSourceType, GuidelineCitation
from copilot.memory.db import session_scope
from copilot.memory.models import GuidelineChunkRow
from copilot.memory.repository import MemoryRepository
from copilot.observability import NoopObservability, Observability, build_observability
from copilot.rag._lexical import bm25_scores, tokenize
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

#: Candidate-window multiplier: at most ``RERANK_WINDOW_MULTIPLIER * top_k``
#: fused candidates reach the rerank stage. This is the retrieval cutoff the
#: pipeline was missing — without it ``fused_candidates`` is the entire corpus
#: (``_dense_rank`` returns every embedded row), so the reranker is not
#: refining a retrieved set, it *is* the retrieval. 4x is the conventional
#: over-fetch: wide enough that a chunk the fused ranking under-rates can still
#: be rescued by the reranker, narrow enough that a weakly-fused chunk cannot
#: be promoted from the corpus's tail straight to a clinician's first citation.
#: It also bounds the rerank payload (latency, and Cohere cost per query).
RERANK_WINDOW_MULTIPLIER = 4


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
    """Hybrid sparse+dense retriever over the guideline corpus.

    ``reranker=None`` means *there is no rerank stage*: the fused sparse+dense
    order is served unmodified. That is the keyless default (see
    :func:`_default_reranker`) and is indistinguishable, by construction, from
    the rerank-failure fallback — one code path, so the degrade case cannot rot
    while the happy path is exercised.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        embedder: Embedder,
        reranker: Reranker | None,
        observability: Observability | None = None,
    ) -> None:
        self._settings = settings
        self._embedder = embedder
        self._reranker = reranker
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
                dense_ids: list[str] = []
                try:
                    query_vec = self._embedder.embed([expanded])[0]
                except Exception:  # best-effort dense leg; retrieval never gates on it
                    # An embedder outage (Voyage 5xx, or a 4xx that exhausts the
                    # retry budget) degrades to sparse-only: rrf_scores(sparse, [])
                    # is already the sparse ranking. Marked on the span so the
                    # outage reads as degradation in the trace, never as silence.
                    _logger.warning(
                        "guideline query embedding failed; serving sparse-only ranking",
                        exc_info=True,
                    )
                    span.set_attribute("dense_degraded", True)
                else:
                    dense_ids = _dense_rank(rows, query_vec)
                sparse_ids = _sparse_rank(rows, expanded)
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
            # RETRIEVE, then rerank: cut the fused union (the whole corpus) to a
            # bounded candidate window first, so the rerank stage refines a
            # retrieved set instead of standing in for retrieval.
            window = fused_candidates[: RERANK_WINDOW_MULTIPLIER * top_k]
            ordered, reranked = self._apply_rerank(expanded, window)
            # The served score must be the score of whichever ranker produced the
            # served order — otherwise a citation's number contradicts its own
            # position. Fused order ⇒ fused RRF scores (already sorted by them);
            # reranked order ⇒ the reranker's ranking, which the Reranker
            # protocol exposes only as an order, so it is read back off the rank.
            served = _reciprocal_rank_scores(ordered) if reranked else scores
            evidence = [_to_evidence(candidate, served) for candidate in ordered[:top_k]]
            span.set_attribute("corpus_chunks", len(rows))
            span.set_attribute("candidates", len(window))
            span.set_attribute("reranked", reranked)
            span.set_attribute("hits", len(evidence))
            span.set_output({"hits": len(evidence), "corpus_chunks": len(rows)})
            return evidence

    def _apply_rerank(
        self, query: str, candidates: list[_Candidate]
    ) -> tuple[list[_Candidate], bool]:
        """Rerank the candidate window; any failure falls back to the fused order.

        Returns the order to serve plus whether the reranker actually produced
        it — the caller needs that to know which ranker's scores to attach.
        ``False`` covers all three fused-order cases: no rerank stage, an empty
        window, and a reranker that raised.
        """
        if self._reranker is None or not candidates:
            return candidates, False
        documents = [_rerank_document(candidate) for candidate in candidates]
        try:
            reranked = self._reranker.rerank(query, documents)
        except Exception:  # the contract: fall back to fused order, never error the answer
            _logger.warning(
                "guideline rerank failed; serving fused sparse+dense order",
                exc_info=True,
            )
            return candidates, False
        return _reorder_to_candidates(reranked, candidates, documents), True


def _default_reranker(settings: Settings) -> Reranker | None:
    """The rerank stage for ``settings``: the real Cohere client, or none at all.

    **This is where "is this reranker real?" is answered, and it is answered by
    the same predicate that builds it.** ``build_reranker`` selects Stub vs.
    Cohere on ``cohere_api_key`` (as ``build_embedder`` does on
    ``voyage_api_key``); reading the same key here to decide whether a rerank
    stage exists keeps one fact in one place, rather than teaching the retriever
    a second, drifting notion of "real".

    Three alternatives were considered and rejected:

    - ``isinstance(reranker, StubReranker)`` in the retriever — special-cases a
      class name, silently mis-classifies any future keyless double, and gives
      the retriever an opinion about implementations it should only know
      through the :class:`~copilot.rag.rerank.Reranker` protocol.
    - A capability flag on the protocol (``reranker.is_semantic``) — the
      honest long-term shape, but it must be declared in ``rerank.py`` and
      implemented on both classes; that file is owned elsewhere right now, so
      reaching into it is out of scope for this fix. This function is the
      single place to swap over if that flag ever lands.
    - Reading ``settings`` inside ``retrieve()`` — ``rerank.py`` states the
      contract that "callers never branch on 'do we have a key?'". Branching in
      the hot path breaks it; branching *here*, at the composition root, honours
      it: the retriever receives ``Reranker | None`` and just asks whether it
      has a stage — a typed, mypy-checked question with no key in sight.

    ``None`` is a real answer, not a missing one: keyless, the correct rerank
    stage is *no rerank stage*, and ``Reranker | None`` says exactly that.
    ``build_reranker`` itself is left alone — it must keep returning a Stub for
    the keyless offline contract its own callers (and the acceptance harness)
    depend on. What changed is that the *retriever* no longer defers to it.
    """
    if not settings.cohere_api_key:
        return None
    return build_reranker(settings)


def build_retriever(
    settings: Settings,
    *,
    embedder: Embedder | None = None,
    reranker: Reranker | None = None,
    observability: Observability | None = None,
) -> GuidelineRetriever:
    """Build the hybrid retriever; ``None`` takes each leg's default from ``settings``.

    ``embedder=None`` selects the keyless stub embedder. ``reranker=None``
    selects :func:`_default_reranker` — the real Cohere reranker when a key is
    configured, and **no rerank stage** when there is not (the keyless stub is
    measurably harmful; see the module docstring).

    An *injected* reranker is always applied. Injection is an explicit act by a
    caller — the harness substituting a recording double, a test proving the
    gate is on capability rather than a blanket "never rerank" — and it would be
    perverse for the factory to accept a collaborator and then ignore it. The
    defect this gate closes lives on the default path: every production call
    site (``routes/chat.py``, the graph's evidence worker) passes no reranker at
    all, so keyless deploys now serve the fused order.

    ``observability`` defaults from ``settings`` exactly as the embedder does,
    so every existing caller emits the ``guideline.retrieve`` span without an
    edit at the call site. Keyless — no Langfuse creds — resolves to
    ``NoopObservability``, so the span is free and behaviour is unchanged.
    """
    return GuidelineRetriever(
        settings=settings,
        embedder=embedder if embedder is not None else build_embedder(settings),
        reranker=reranker if reranker is not None else _default_reranker(settings),
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
    """Boost hits by *how much* of their section heading the query matches.

    The heading path is a strong topical signal, so a chunk already retrieved by
    sparse/dense ranking whose section shares key terms with the (de-identified,
    expanded) query is promoted by a bounded amount. Only ids already in
    ``scores`` are touched — the boost re-prioritises hits, never resurrects an
    unmatched chunk. Deterministic: a plain dict copy with additive boosts.

    **Scaled by heading coverage** (``matched terms / heading terms``), not a
    flat all-or-nothing step. The step function computed the very thing it then
    threw away: it knew *which* heading terms matched and rounded that to a
    single bit, so a heading the query names outright and a heading brushing it
    with one incidental word were promoted identically. Measured on the shipped
    corpus, that tie is the P1:

        "How do I reverse warfarin in major life-threatening bleeding?"
          major-bleeding-on-warfarin             matches 3/3 of its heading
          supratherapeutic-inr-without-bleeding  matches 1/4 ("bleeding")

    Both scored an *identical* flat boost, leaving an exact RRF tie to be broken
    by dict insertion order — which is sparse-first, so the INR-hold section won
    a coin toss it should have lost 3-to-1. Coverage also demotes accidental
    heading collisions generally: a sepsis fluids question no longer gets AKI's
    "Initial evaluation" promoted on the strength of the word "initial" alone
    (1/2 coverage, half the boost). ``SECTION_MATCH_BOOST`` remains the ceiling,
    reached only by a heading the query matches in full, so the bound the
    constant documents still holds.
    """
    query_terms = set(tokenize(query))
    if not query_terms:
        return scores
    boosted = dict(scores)
    for chunk_id in boosted:
        candidate = candidates.get(chunk_id)
        if candidate is None:
            continue
        section_terms = set(tokenize(candidate.section))
        if not section_terms:
            continue
        matched = len(section_terms & query_terms)
        if matched:
            boosted[chunk_id] += SECTION_MATCH_BOOST * (matched / len(section_terms))
    return boosted


def _sparse_rank(rows: list[GuidelineChunkRow], query: str) -> list[str]:
    """BM25 sparse ranking over the retrieved chunks (both backends).

    Chunks sharing no query term score ``0.0`` and are dropped, so the ranking
    stays a *retrieval* result — a candidate set — rather than a total order
    over the corpus. Rows arrive id-ascending and the sort is stable, so equal
    scores keep a deterministic order.
    """
    scores = bm25_scores(tokenize(query), {str(row.id): row.content for row in rows})
    matches = [(chunk_id, score) for chunk_id, score in scores.items() if score > 0.0]
    matches.sort(key=lambda pair: -pair[1])
    return [chunk_id for chunk_id, _score in matches]


def _rerank_document(candidate: _Candidate) -> str:
    """The text a reranker scores: the section heading, then the chunk body.

    The body alone made the reranker structurally blind to ``section`` — the
    one signal :func:`_boost_section_matches` had just used to re-prioritise the
    fused ranking. A reranker that cannot see the heading cannot agree with the
    boost, so it could only ever overwrite it. Heading first, blank line, body:
    the shape guideline text already has on the page, and the shape rerank
    models are trained on (Cohere's own guidance is to include titles in the
    document string). Safe to send — ``section`` is a corpus heading slug that
    never passed through a patient record, and the query reached here via the
    ``deidentify`` choke point.
    """
    return f"{candidate.section}\n\n{candidate.content}"


def _reciprocal_rank_scores(ordered: Sequence[_Candidate]) -> dict[str, float]:
    """Score each candidate by its *served* rank: ``1/(k + rank)``.

    Used when a reranker produced the order. The :class:`Reranker` protocol
    returns a permutation and no scores, so the reranker's own relevance
    numbers are not available to serve; its ranking is. This projects that
    ranking onto the same reciprocal-rank scale the fused score is already
    measured in (:func:`rrf_scores` sums exactly these terms), so
    ``GuidelineEvidence.score`` stays one comparable quantity across both paths
    and is monotonically non-increasing by construction.

    Keeping the *fused* score here instead is what made the served scores
    non-monotonic: the reranked order is not sorted by the fused score, so a
    clinician's first citation could carry a number lower than the third's.
    Re-sorting the fused scores onto the served positions would be worse — it
    would attach a score the candidate did not earn.
    """
    return {
        candidate.chunk_id: 1.0 / (RRF_K + rank)
        for rank, candidate in enumerate(ordered, start=1)
    }


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
