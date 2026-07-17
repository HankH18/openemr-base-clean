"""The keyless stub reranker must not overwrite the fused ranking.

`GuidelineRetriever.retrieve` fused sparse+dense with RRF, added a bounded
section-heading boost — and then handed the whole thing to `_apply_rerank`,
whose return value is a TOTAL reorder. Keyless (the shipped deploy: DEPLOY.md
leaves `VOYAGE_API_KEY`/`COHERE_API_KEY` unset, `docker-compose.deploy.yml`
defaults them empty), `build_reranker` yields `StubReranker`, which sorts by
`_lexical.overlap_score` — a raw term-frequency sum: no IDF, no stemming, no
length normalization. That stub ran LAST, so it, not the hybrid pipeline, chose
what a clinician reads first.

Measured over the shipped `corpus/` (19 chunks, keyless, production retriever
vs. an identity reranker): the stub changed top-1 on 2 of 7 clinical queries
and was wrong both times — 0 wins, 2 losses. The headline case is live:

    "What is the MAP target for septic shock vasopressors?"
      fused (RRF+boost) top1 : vasopressors-and-map-target   rrf=0.047643
      served                 : recognition-and-screening     rrf=0.032787

0.047643 == 0.03125 (sparse+dense) + 1/61 (`SECTION_MATCH_BOOST`) — the boost
is exactly what lifted the right chunk to #1, and the reranker is exactly what
threw it away. It could not have done otherwise: `_apply_rerank` passed
`candidate.content` only, so the reranker never saw `section`, the signal the
boost encodes.

Re-measured after the fix, the attribution is more specific than "the stub is
strictly harmful", and the tests below say so: **feeding the section rescues the
MAP query even with the stub applied** — the heading's own terms then count
toward `overlap_score`. Section blindness was the proximate cause of THAT
query. With the section fed and the window bounded, the stub still changes
top-1 on 1 of 7 and is still wrong (`What fluids for sepsis resuscitation?`):
0 wins, 1 loss. Never better, sometimes worse — which is why it is gated out
rather than merely fed better input.

These tests pin the contract: the rerank stage exists only when a real, keyed
reranker does; a reranker that IS applied sees the section; the candidate window
is bounded before rerank (retrieval must retrieve before it reranks); the served
`.score` matches the served order; and the pre-existing degrade-on-failure guard
still holds.

They run against the REAL `corpus/` through the REAL ingest path, because the
defect is a property of the real corpus's ranking, not of a hand-built fixture
that could be shaped to agree with whatever the code happens to do.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Sequence
from itertools import pairwise
from pathlib import Path

import pytest
import sqlalchemy as sa

from copilot.config import Settings, get_settings
from copilot.rag.embeddings import StubEmbedder
from copilot.rag.rerank import CohereReranker, RerankError, StubReranker
from copilot.rag.retriever import (
    RERANK_WINDOW_MULTIPLIER,
    GuidelineEvidence,
    _default_reranker,
    build_retriever,
)

CORPUS_DIR = Path(__file__).resolve().parents[1] / "corpus"

#: The live defect, verbatim: graph mode is on in production
#: (`COPILOT_CHAT_GRAPH_ENABLED=true`) and routes chat through
#: `graph/evidence_retriever.py` at `top_k=4`.
MAP_QUERY = "What is the MAP target for septic shock vasopressors?"
MAP_SECTION = "vasopressors-and-map-target"
WRONG_SECTION = "recognition-and-screening"
LIVE_TOP_K = 4

#: The query on which the stub reranker STILL overrules the fused ranking, and
#: is still wrong, even once it can see the section. See
#: `test_the_stub_reranker_still_loses_even_when_it_can_see_the_section`.
FLUIDS_QUERY = "What fluids for sepsis resuscitation?"
FLUIDS_SECTION = "initial-resuscitation"

#: The 7 probe queries the 0-wins/2-losses measurement was taken over.
PROBE_QUERIES = [
    MAP_QUERY,
    "How much insulin infusion for DKA?",
    "When do I stop the insulin drip in DKA?",
    "How do I reverse warfarin for major bleeding?",
    "What fluids for sepsis resuscitation?",
    "Which nephrotoxins should I hold in AKI?",
    "When is dialysis indicated in acute kidney injury?",
]


# --- fixtures ---------------------------------------------------------------


def _clear_db_caches() -> None:
    from copilot.memory.db import get_engine, get_session_factory

    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()


@pytest.fixture
def rag_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Temp-file SQLite DB with the guideline tables pre-created; caches cleared."""
    db_file = tmp_path / "rerank_gating.db"
    monkeypatch.setenv("COPILOT_DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
    _clear_db_caches()

    import copilot.memory.models  # noqa: F401  (registers tables on Base.metadata)
    from copilot.memory.db import Base

    engine = sa.create_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(engine)
    engine.dispose()
    yield
    _clear_db_caches()


@pytest.fixture
async def real_corpus(rag_db: None) -> int:
    """Ingest the shipped `corpus/` exactly as the deploy does. Returns chunk count."""
    from copilot.memory.db import session_scope
    from copilot.rag.ingest import ingest_corpus

    async with session_scope() as session:
        report = await ingest_corpus(session, StubEmbedder(), corpus_dir=CORPUS_DIR)
    assert report.chunks_ingested > 0, "the shipped corpus must ingest — else these tests are vacuous"
    return report.chunks_ingested


# --- doubles ----------------------------------------------------------------


class _IdentityReranker:
    """Applied, but order-preserving: whatever it is handed IS the fused order."""

    def __init__(self) -> None:
        self.documents: list[list[str]] = []

    def rerank(self, query: str, documents: Sequence[str]) -> list[str]:
        docs = [str(d) for d in documents]
        self.documents.append(docs)
        return docs


class _ReversingReranker:
    """A stand-in for a REAL (keyed) reranker: it reorders, unmistakably.

    Reversal is chosen because no fused ranking of a non-trivial corpus is its
    own reverse, so "was this applied?" cannot pass by coincidence.
    """

    def rerank(self, query: str, documents: Sequence[str]) -> list[str]:
        return [str(d) for d in reversed(list(documents))]


class _BoomReranker:
    """A reranker in outage — `CohereReranker` raises `RerankError` on 5xx."""

    def __init__(self) -> None:
        self.calls = 0

    def rerank(self, query: str, documents: Sequence[str]) -> list[str]:
        self.calls += 1
        raise RerankError("cohere 5xx / connection error")


def _settings(**overrides: str) -> Settings:
    return Settings(database_url="sqlite+aiosqlite:///:memory:", **overrides)


def _sections(evidence: Sequence[GuidelineEvidence]) -> list[str]:
    return [item.section for item in evidence]


def _ids(evidence: Sequence[GuidelineEvidence]) -> list[str]:
    return [item.chunk_id for item in evidence]


# --- the headline: the live defect ------------------------------------------


async def test_map_target_query_serves_the_map_chunk_first_on_the_keyless_path(
    real_corpus: int,
) -> None:
    """THE live defect: a hospitalist asking the MAP target got the wrong chunk.

    Keyless + graph mode + top_k=4 is exactly what runs in production. Pre-fix
    this served `recognition-and-screening`; the chunk that actually carries the
    MAP target ranked 3rd.
    """
    evidence = await build_retriever(get_settings()).retrieve(MAP_QUERY, top_k=LIVE_TOP_K)

    assert evidence, "retrieval over the shipped corpus must return evidence"
    assert evidence[0].section == MAP_SECTION, (
        f"the MAP-target chunk must be served FIRST; got {_sections(evidence)}"
    )
    # Not merely "first among four": the answer must survive a tighter top_k,
    # where pre-fix the right chunk was not returned AT ALL.
    tight = await build_retriever(get_settings()).retrieve(MAP_QUERY, top_k=2)
    assert _sections(tight)[0] == MAP_SECTION, f"top_k=2 must still lead with it: {_sections(tight)}"
    # And it is real, citable evidence — not a hollow placeholder.
    top = evidence[0]
    assert "65" in top.content, f"the MAP-target chunk must carry the target: {top.content!r}"
    assert top.citation.field_or_chunk_id == top.chunk_id
    assert top.citation.page_or_section == MAP_SECTION


# --- the gate: no stub reorder, but a real reranker IS applied ---------------


async def test_keyless_order_equals_the_fused_order_over_every_probe_query(
    real_corpus: int,
) -> None:
    """The keyless path serves the fused (RRF+boost) ranking — the stub cannot reorder it.

    The identity reranker is *applied* (injection is explicit), and identity
    preserves whatever it is handed, so its output IS the fused order. The
    keyless default must equal it on every query — not just the MAP one.
    """
    for query in PROBE_QUERIES:
        identity = _IdentityReranker()
        fused = await build_retriever(get_settings(), reranker=identity).retrieve(
            query, top_k=LIVE_TOP_K
        )
        keyless = await build_retriever(get_settings()).retrieve(query, top_k=LIVE_TOP_K)

        assert identity.documents, "the injected reranker must actually have been called"
        assert _ids(keyless) == _ids(fused), (
            f"keyless retrieval must serve the fused order for {query!r}: "
            f"got {_sections(keyless)}, fused {_sections(fused)}"
        )


def test_the_keyless_default_has_no_rerank_stage_at_all() -> None:
    """Expressed as `Reranker | None` at the composition root, not a runtime isinstance."""
    assert _default_reranker(_settings()) is None
    assert build_retriever(_settings())._reranker is None


def test_a_configured_key_selects_the_real_cohere_reranker() -> None:
    """The gate is on capability: a key means a real reranker, and it is built."""
    resolved = _default_reranker(_settings(cohere_api_key="ck-test"))
    assert isinstance(resolved, CohereReranker)
    assert isinstance(build_retriever(_settings(cohere_api_key="ck-test"))._reranker, CohereReranker)


async def test_a_real_reranker_is_applied_not_suppressed(real_corpus: int) -> None:
    """Not "never rerank": a real reranker's order is served.

    Guards the obvious over-correction — deleting the rerank stage outright
    would pass every "the stub must not reorder" test above while silently
    disabling the Cohere refinement keyed deploys pay for.
    """
    fused = await build_retriever(get_settings(), reranker=_IdentityReranker()).retrieve(
        MAP_QUERY, top_k=LIVE_TOP_K
    )
    reranked = await build_retriever(get_settings(), reranker=_ReversingReranker()).retrieve(
        MAP_QUERY, top_k=LIVE_TOP_K
    )

    assert _ids(reranked) != _ids(fused), "a real reranker's reordering must reach the served order"
    assert _ids(reranked)[0] != _ids(fused)[0], "including the top-1 a clinician reads first"


async def test_the_stub_reranker_still_loses_even_when_it_can_see_the_section(
    real_corpus: int,
) -> None:
    """The evidence the gate rests on, pinned — and measured, not assumed.

    Feeding the section (the fix above) *rescues the MAP query even with the
    stub applied*: the heading slug's terms — "vasopressors", "map", "target" —
    now count toward `overlap_score`, so the stub agrees with the boost there.
    The section blindness, not the stub alone, was what sank that query.

    The stub is still not a ranker worth deferring to. Re-measured over the
    shipped corpus WITH the section fed and the window bounded, it changes top-1
    on 1 of the 7 probe queries and is still wrong: on "What fluids for sepsis
    resuscitation?" the fused ranking correctly leads with
    `initial-resuscitation` and the stub replaces it with
    `recognition-and-screening`. 0 wins, 1 loss — better than 0/2, still
    strictly negative. A raw term-frequency sum with no IDF and no length
    normalization has no business overruling a principled fusion, and this is
    what that costs in practice.

    If this ever goes green because the stub improved, re-evaluate the gate in
    `_default_reranker` — do not delete this test. It is the gate's
    justification, not an endorsement of the defect.
    """
    fused = await build_retriever(get_settings(), reranker=_IdentityReranker()).retrieve(
        FLUIDS_QUERY, top_k=LIVE_TOP_K
    )
    stubbed = await build_retriever(get_settings(), reranker=StubReranker()).retrieve(
        FLUIDS_QUERY, top_k=LIVE_TOP_K
    )

    assert fused[0].section == FLUIDS_SECTION, (
        f"the fused ranking gets this right: {_sections(fused)}"
    )
    assert stubbed[0].section == WRONG_SECTION, (
        f"and the stub is what throws it away: {_sections(stubbed)}"
    )


async def test_the_keyless_path_serves_the_fused_answer_the_stub_would_have_lost(
    real_corpus: int,
) -> None:
    """The gate's payoff on the one query the stub still gets wrong."""
    evidence = await build_retriever(get_settings()).retrieve(FLUIDS_QUERY, top_k=LIVE_TOP_K)
    assert evidence[0].section == FLUIDS_SECTION, f"got {_sections(evidence)}"


# --- the reranker must see the section --------------------------------------


async def test_an_applied_reranker_is_handed_the_section_not_just_the_body(
    real_corpus: int,
) -> None:
    """A reranker blind to `section` cannot agree with the section boost.

    `_apply_rerank` passed `candidate.content` alone, so the reranker never saw
    the one signal `_boost_section_matches` had just used — it could only
    overwrite it.
    """
    identity = _IdentityReranker()
    await build_retriever(get_settings(), reranker=identity).retrieve(MAP_QUERY, top_k=LIVE_TOP_K)

    documents = identity.documents[0]
    assert any(doc.startswith(f"{MAP_SECTION}\n\n") for doc in documents), (
        f"the section heading must lead each rerank document; got {documents[:1]}"
    )
    for doc in documents:
        heading, separator, body = doc.partition("\n\n")
        assert separator, f"every rerank document must carry a heading + body: {doc!r}"
        assert heading and "\n" not in heading, f"heading must be the section slug: {heading!r}"
        assert body, f"the chunk body must still be sent: {doc!r}"


# --- retrieval must retrieve before it reranks ------------------------------


async def test_the_candidate_window_is_bounded_before_rerank(real_corpus: int) -> None:
    """`_dense_rank` returns every embedded row, so the fused union is the CORPUS.

    Unbounded, the reranker is handed all 19 chunks — it is not refining a
    retrieved set, it IS the retrieval. The window caps it at
    `RERANK_WINDOW_MULTIPLIER * top_k`.
    """
    top_k = 2
    identity = _IdentityReranker()
    await build_retriever(get_settings(), reranker=identity).retrieve(MAP_QUERY, top_k=top_k)

    handed = len(identity.documents[0])
    limit = RERANK_WINDOW_MULTIPLIER * top_k
    assert handed <= limit, f"the reranker must see at most {limit} candidates, got {handed}"
    assert handed < real_corpus, (
        f"the whole corpus ({real_corpus} chunks) must not reach the reranker, got {handed}"
    )


async def test_the_window_never_starves_the_requested_top_k(real_corpus: int) -> None:
    """The cutoff bounds the rerank input; it must not shrink the answer."""
    evidence = await build_retriever(get_settings()).retrieve(MAP_QUERY, top_k=LIVE_TOP_K)
    assert len(evidence) == LIVE_TOP_K, f"top_k={LIVE_TOP_K} must still be served in full"


# --- the served score must match the served order ---------------------------


async def test_served_scores_are_monotonically_non_increasing_on_the_keyless_path(
    real_corpus: int,
) -> None:
    """A citation's number must not contradict its own position.

    Pre-fix the served scores read [0.032787, 0.032258, 0.047643, 0.031746] for
    the MAP query: the third citation carried the highest score.
    """
    for query in PROBE_QUERIES:
        evidence = await build_retriever(get_settings()).retrieve(query, top_k=LIVE_TOP_K)
        scores = [item.score for item in evidence]
        assert all(a >= b for a, b in pairwise(scores)), (
            f"served scores must be non-increasing for {query!r}: {scores}"
        )
        assert all(score > 0.0 for score in scores), f"served evidence must score above zero: {scores}"


async def test_served_scores_are_monotonic_when_a_real_reranker_reorders(
    real_corpus: int,
) -> None:
    """The reranked order is not sorted by the FUSED score — so the score must follow the order."""
    evidence = await build_retriever(get_settings(), reranker=_ReversingReranker()).retrieve(
        MAP_QUERY, top_k=LIVE_TOP_K
    )

    scores = [item.score for item in evidence]
    assert all(a >= b for a, b in pairwise(scores)), (
        f"a reranked order's served scores must be non-increasing: {scores}"
    )
    assert all(score > 0.0 for score in scores), f"reranked evidence must score above zero: {scores}"


# --- the pre-existing degrade-on-failure guard still holds ------------------


async def test_a_failing_reranker_still_degrades_to_the_fused_order(
    real_corpus: int, caplog: pytest.LogCaptureFixture
) -> None:
    """No regression: a reranker outage falls back to fused order, logged, never raised."""
    caplog.set_level(logging.WARNING)
    boom = _BoomReranker()

    fused = await build_retriever(get_settings(), reranker=_IdentityReranker()).retrieve(
        MAP_QUERY, top_k=LIVE_TOP_K
    )
    degraded = await build_retriever(get_settings(), reranker=boom).retrieve(
        MAP_QUERY, top_k=LIVE_TOP_K
    )

    assert boom.calls == 1, "the rerank must actually have been attempted"
    assert _ids(degraded) == _ids(fused), (
        f"a rerank failure must serve the fused order: {_sections(degraded)} vs {_sections(fused)}"
    )
    assert any("rerank" in record.getMessage().lower() for record in caplog.records), (
        "the rerank fallback must be logged — no silent degradation"
    )


async def test_a_failing_reranker_still_serves_the_right_map_chunk(real_corpus: int) -> None:
    """The degrade path is the keyless path: both serve fused, so both are correct."""
    degraded = await build_retriever(get_settings(), reranker=_BoomReranker()).retrieve(
        MAP_QUERY, top_k=LIVE_TOP_K
    )
    assert degraded[0].section == MAP_SECTION, f"got {_sections(degraded)}"


async def test_an_empty_corpus_still_yields_no_evidence(rag_db: None) -> None:
    """The empty-corpus contract survives the gate: [], never a fabricated cite."""
    assert await build_retriever(get_settings()).retrieve(MAP_QUERY, top_k=LIVE_TOP_K) == []
