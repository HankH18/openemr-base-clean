"""The dense leg is best-effort: an embedder outage degrades to sparse-only.

`GuidelineRetriever.retrieve` calls the embedder to build the query vector. That
call is a network egress (Voyage), and `VoyageEmbedder` raises `EmbeddingError`
by design once its retry budget is exhausted. Unguarded, that exception escapes
`retrieve()` -> the graph's evidence-retriever -> the supervisor -> the chat
service -> the route, where the middleware logs and re-raises it as an HTTP 500.
Graph mode is ON in production, so a Voyage outage would turn every
guideline-intent question into a 500 — while the corpus's sparse leg could have
answered it unaided.

These tests pin the contract stated in the module docstring: retrieval is gated
on the corpus, never on a network call. The dense leg failing must DEGRADE the
hybrid to sparse-only, not fail the answer — so each test asserts real evidence
still comes back, not merely that nothing raised.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from copilot.config import get_settings
from copilot.observability import NoopObservability, Span
from copilot.rag.embeddings import EmbeddingError, StubEmbedder
from copilot.rag.retriever import build_retriever

# --- fixtures ---------------------------------------------------------------


def _clear_db_caches() -> None:
    from copilot.memory.db import get_engine, get_session_factory

    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()


@pytest.fixture
def rag_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Temp-file SQLite DB with the guideline tables pre-created; caches cleared."""
    db_file = tmp_path / "dense_degrade.db"
    monkeypatch.setenv("COPILOT_DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
    _clear_db_caches()

    import copilot.memory.models  # noqa: F401  (registers tables on Base.metadata)
    from copilot.memory.db import Base

    engine = sa.create_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(engine)
    engine.dispose()
    yield
    _clear_db_caches()


class _RaisingEmbedder:
    """An embedder in outage: every call raises, as VoyageEmbedder does on 5xx.

    `EmbeddingError` is the real, by-design failure of `VoyageEmbedder` once the
    retry budget is exhausted (`embeddings.py`), so this double reproduces the
    production failure rather than a synthetic one.
    """

    def __init__(self) -> None:
        self.calls = 0

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls += 1
        raise EmbeddingError("voyage 5xx / connection error")


class _RecordingSpan:
    """Captures span attributes so the degradation signal can be asserted."""

    def __init__(self) -> None:
        self.attributes: dict[str, Any] = {}
        self.output: Any = None

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value

    def set_output(self, value: Any) -> None:
        self.output = value


class _CapturingObservability(NoopObservability):
    """A no-op backend that hands out — and keeps — recording spans.

    Subclasses the real `NoopObservability` so every other method of the
    `Observability` protocol keeps its production no-op behaviour; only `span`
    is overridden, to retain what the retriever recorded.
    """

    def __init__(self) -> None:
        self.spans: dict[str, _RecordingSpan] = {}

    @asynccontextmanager
    async def span(self, name: str, **attributes: Any) -> AsyncIterator[Span]:
        recorded = _RecordingSpan()
        recorded.attributes.update(attributes)
        self.spans[name] = recorded
        yield recorded


async def _seed(chunks: list[tuple[str, str]]) -> None:
    """Seed a guideline doc + chunks, each with a real (stub) stored embedding."""
    from copilot.memory.db import session_scope
    from copilot.memory.repository import MemoryRepository

    stub = StubEmbedder()
    async with session_scope() as session:
        repo = MemoryRepository(session)
        doc = await repo.create_guideline_document(
            title="Test guideline", source="test:dense-degrade", license="CC-BY-4.0"
        )
        for index, (section, content) in enumerate(chunks):
            await repo.create_guideline_chunk(
                guideline_document_id=doc.id,
                content=content,
                section=section,
                chunk_index=index,
                embedding=stub.embed([content])[0],
            )


_CORPUS = [
    ("insulin-therapy", "Continuous intravenous insulin infusion for diabetic ketoacidosis."),
    ("nephrotoxin-stewardship", "Hold nephrotoxins in acute kidney injury; avoid NSAIDs."),
    ("lactate", "Remeasure lactate in sepsis and start antibiotics within one hour."),
]

# --- the degradation contract -----------------------------------------------


async def test_embedder_outage_does_not_propagate_out_of_retrieve(rag_db: None) -> None:
    """A raising embedder must not escape `retrieve()` as an exception."""
    await _seed(_CORPUS)
    embedder = _RaisingEmbedder()
    retriever = build_retriever(get_settings(), embedder=embedder)

    # Pre-fix this raised EmbeddingError straight out of retrieve().
    results = await retriever.retrieve("insulin infusion for DKA", top_k=3)

    assert embedder.calls == 1, "the dense leg must actually have been attempted"
    assert isinstance(results, list)


async def test_embedder_outage_still_returns_the_sparse_matched_chunk(rag_db: None) -> None:
    """It DEGRADES, not just "doesn't crash": sparse-only still answers.

    The query's terms overlap only the insulin chunk, so the sparse leg alone is
    enough to retrieve it. If the guard merely swallowed the error and returned
    [], this test would fail — dropping every piece of evidence an unaided sparse
    leg could have served is the same outage in a quieter costume.
    """
    await _seed(_CORPUS)
    retriever = build_retriever(get_settings(), embedder=_RaisingEmbedder())

    results = await retriever.retrieve("intravenous insulin infusion", top_k=3)

    assert results, "sparse-only retrieval must still return evidence during a dense outage"
    sections = [evidence.section for evidence in results]
    assert "insulin-therapy" in sections
    top = results[0]
    assert top.section == "insulin-therapy", f"sparse-matched chunk must rank first, got {sections}"
    assert "insulin infusion" in top.content
    # Real, typed, citable evidence — not a hollow placeholder.
    assert top.score > 0.0
    assert top.citation.field_or_chunk_id == top.chunk_id
    assert top.citation.quote_or_value == top.content


async def test_embedder_outage_marks_the_span_as_degraded(rag_db: None) -> None:
    """The outage is visible in the trace — degraded, never silent."""
    await _seed(_CORPUS)
    obs = _CapturingObservability()
    retriever = build_retriever(
        get_settings(), embedder=_RaisingEmbedder(), observability=obs
    )

    await retriever.retrieve("intravenous insulin infusion", top_k=3)

    span = obs.spans["guideline.retrieve"]
    assert span.attributes.get("dense_degraded") is True


async def test_working_embedder_path_is_unchanged(rag_db: None) -> None:
    """No regression: a healthy embedder retrieves exactly as it did before.

    Pinned against the un-guarded behaviour by construction — the guard only ever
    runs on the exception path, so a working embedder must produce byte-identical
    hybrid results.
    """
    await _seed(_CORPUS)
    retriever = build_retriever(get_settings(), embedder=StubEmbedder())

    results = await retriever.retrieve("intravenous insulin infusion", top_k=3)

    assert results, "the healthy hybrid path must return evidence"
    assert results[0].section == "insulin-therapy"
    assert "insulin infusion" in results[0].content
    # Deterministic across calls, and the span records no degradation.
    obs = _CapturingObservability()
    again = await build_retriever(
        get_settings(), embedder=StubEmbedder(), observability=obs
    ).retrieve("intravenous insulin infusion", top_k=3)
    assert [e.chunk_id for e in again] == [e.chunk_id for e in results]
    assert "dense_degraded" not in obs.spans["guideline.retrieve"].attributes


async def test_dense_outage_and_empty_corpus_still_yields_no_evidence(rag_db: None) -> None:
    """The empty-corpus contract survives the guard: [] , never a fabricated cite."""
    retriever = build_retriever(get_settings(), embedder=_RaisingEmbedder())
    assert await retriever.retrieve("intravenous insulin infusion", top_k=3) == []
