"""A bare, unlabelled patient name must never reach the embedder or reranker.

`deidentify` is the retriever's PHI choke point, but it scrubs identifiers by
SHAPE — and a bare, unlabelled name has no shape to match. Its own module
docstring states the residual outright: ``"Should John Doe get a statin?"``
passes through UNCHANGED, so ``John Doe`` would egress verbatim to Voyage
(embedder) and Cohere (reranker) the moment either key is configured.

These tests pin the second guard: `retrieve()` distills the (de-identified,
expanded) query down to only recognised clinical terms BEFORE egress, so a name
the regex could not catch is dropped from what leaves the process — while a real
clinical term (``statin``) still goes out, on both the embedder AND the reranker
egress legs.

Doubles RECORD exactly the text they are handed, so the assertion is about the
real outbound payload, not a proxy. Pre-fix (the retriever embedding/reranking
the raw expanded text) these are RED: the recorded egress contains ``John Doe``.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest
import sqlalchemy as sa

from copilot.config import get_settings
from copilot.rag.retriever import build_retriever

# The residual `deidentify` cannot catch: a bare, unlabelled first+last name with
# no label and no separator. See copilot/rag/deidentify.py's module docstring.
NAME_TOKENS = ("john", "doe")
CLINICAL_QUERY = "Should John Doe get a statin?"


def _clear_db_caches() -> None:
    from copilot.memory.db import get_engine, get_session_factory

    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()


@pytest.fixture
def rag_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Temp-file SQLite DB with the guideline tables pre-created; caches cleared."""
    db_file = tmp_path / "distill_egress.db"
    monkeypatch.setenv("COPILOT_DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
    _clear_db_caches()

    import copilot.memory.models  # noqa: F401  (registers tables on Base.metadata)
    from copilot.memory.db import Base

    engine = sa.create_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(engine)
    engine.dispose()
    yield
    _clear_db_caches()


class _RecordingEmbedder:
    """Records exactly the texts handed to the embedder (the Voyage egress leg)."""

    def __init__(self) -> None:
        self.texts: list[str] = []

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.texts.extend(texts)
        # A real, non-degenerate vector per input so the dense leg does not degrade.
        return [[float(len(text) + 1)] * 8 for text in texts]


class _RecordingReranker:
    """Records the query handed to the reranker (the Cohere egress leg); identity order."""

    def __init__(self) -> None:
        self.queries: list[str] = []

    def rerank(self, query: str, documents: Sequence[str]) -> list[str]:
        self.queries.append(query)
        return list(documents)


async def _seed_corpus_with_statin() -> None:
    """Seed a guideline chunk whose text carries ``statin`` (the query's real term)."""
    from copilot.memory.db import session_scope
    from copilot.memory.repository import MemoryRepository

    embedder = _RecordingEmbedder()
    async with session_scope() as session:
        repo = MemoryRepository(session)
        doc = await repo.create_guideline_document(
            title="Lipids", source="test:distill", license="CC-BY-4.0"
        )
        for index, (section, content) in enumerate(
            [
                (
                    "statin-therapy",
                    "Start a high-intensity statin for secondary prevention after "
                    "atherosclerotic cardiovascular disease.",
                ),
                (
                    "monitoring",
                    "Recheck a lipid panel and liver enzymes after starting a statin.",
                ),
            ]
        ):
            await repo.create_guideline_chunk(
                guideline_document_id=doc.id,
                content=content,
                section=section,
                chunk_index=index,
                embedding=embedder.embed([content])[0],
            )


async def test_bare_name_never_reaches_the_embedder(rag_db: None) -> None:
    """The recorded embedder payload must not carry the bare name — but must keep the term."""
    await _seed_corpus_with_statin()
    embedder = _RecordingEmbedder()
    retriever = build_retriever(get_settings(), embedder=embedder)

    await retriever.retrieve(CLINICAL_QUERY, top_k=2)

    assert embedder.texts, "the retriever must embed the query at retrieve() time"
    egress = " ".join(embedder.texts).lower()
    for token in NAME_TOKENS:
        assert token not in egress, (
            f"the bare name token {token!r} leaked to the embedder (Voyage): {embedder.texts!r}"
        )
    assert "statin" in egress, (
        f"distillation dropped the real clinical term too: {embedder.texts!r}"
    )


async def test_bare_name_never_reaches_the_reranker(rag_db: None) -> None:
    """The recorded reranker query must not carry the bare name — but must keep the term."""
    await _seed_corpus_with_statin()
    reranker = _RecordingReranker()
    retriever = build_retriever(
        get_settings(), embedder=_RecordingEmbedder(), reranker=reranker
    )

    await retriever.retrieve(CLINICAL_QUERY, top_k=2)

    assert reranker.queries, "the injected reranker must actually have been called"
    egress = " ".join(reranker.queries).lower()
    for token in NAME_TOKENS:
        assert token not in egress, (
            f"the bare name token {token!r} leaked to the reranker (Cohere): {reranker.queries!r}"
        )
    assert "statin" in egress, (
        f"distillation dropped the real clinical term too: {reranker.queries!r}"
    )
