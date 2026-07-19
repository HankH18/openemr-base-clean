"""A genuine no-match query must return ``[]`` — never an unrelated fabricated cite.

The retriever's whole honesty contract (``retriever.py`` module docstring;
``GuidelineRetriever.retrieve`` docstring) is: *"an empty corpus returns ``[]``
— explicit no-evidence, never a fabricated cite."* That held only for an EMPTY
corpus. Against a NON-empty corpus, a question the corpus has nothing to say
about still came back with four cited chunks, because the dense leg
(``_dense_rank``) returned **every** embedded row regardless of cosine —
including cosine ``0.0`` — while the sparse leg (``_sparse_rank``) already
dropped its zero-BM25 chunks. So RRF always received a full dense ranking and
``retrieve()`` was never ``[]`` for a populated corpus, no matter how off-topic
the query.

Two shapes of genuine no-match are pinned here, both against a real seeded
corpus through the real keyless retriever:

* an **off-corpus clinical** question — ``"How do I treat HTN?"`` distills to the
  real clinical terms ``htn hypertension`` (so this is NOT the trivial
  distills-to-empty case), but no corpus chunk is about hypertension, so neither
  leg matches;
* a **pure-name** question — ``"Should John Doe be discharged today?"`` carries a
  bare name ``deidentify`` cannot scrub and no clinical term, so it distills to
  ``""``.

Pre-fix both returned 4 cites (RED: ``assert ... == []`` fails). Post-fix
``_dense_rank`` drops ``cosine <= 0.0`` chunks (mirroring ``_sparse_rank``'s
zero-drop), so a true no-match fuses to empty and ``retrieve()`` returns ``[]``.

Scope guard: this closes the ZERO-overlap case only — it does NOT add an
absolute minimum-score threshold. The in-corpus positive control below proves
the drop does not over-reach: a genuinely matching query still returns its
section.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest
import sqlalchemy as sa

from copilot.config import get_settings
from copilot.rag.embeddings import StubEmbedder
from copilot.rag.retriever import build_retriever

# Off-corpus clinical: distills to real clinical terms (`htn hypertension`) that
# no seeded chunk carries — verified zero cosine AND zero BM25 on the corpus below.
OFF_CORPUS_CLINICAL_QUERY = "How do I treat HTN?"
# Pure name: the residual `deidentify` cannot catch (see its docstring / the
# distill-egress test) plus only non-clinical words -> distills to "".
PURE_NAME_QUERY = "Should John Doe be discharged today?"
# In-corpus positive control: overlaps the sepsis chunk on both legs.
IN_CORPUS_QUERY = "How much crystalloid for septic shock and antibiotics?"

# A deliberately narrow corpus (DKA / AKI / sepsis / warfarin) so "hypertension"
# and the bare name are genuinely off-corpus on both the sparse and dense legs.
_CORPUS: list[tuple[str, str]] = [
    (
        "insulin-therapy",
        "Continuous intravenous insulin infusion for diabetic ketoacidosis; "
        "target a glucose decline until the anion gap closes.",
    ),
    (
        "nephrotoxin-stewardship",
        "Hold nephrotoxins in acute kidney injury; avoid NSAIDs and "
        "aminoglycosides and dose renally cleared drugs.",
    ),
    (
        "initial-resuscitation",
        "Give balanced crystalloid for septic shock and measure lactate; "
        "start broad-spectrum antibiotics within one hour.",
    ),
    (
        "major-bleeding-on-warfarin",
        "For major bleeding on warfarin give four-factor prothrombin complex "
        "concentrate together with intravenous vitamin K.",
    ),
]


def _clear_db_caches() -> None:
    from copilot.memory.db import get_engine, get_session_factory

    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()


@pytest.fixture
def rag_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Temp-file SQLite DB with the guideline tables pre-created; caches cleared."""
    db_file = tmp_path / "no_match.db"
    monkeypatch.setenv("COPILOT_DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
    _clear_db_caches()

    import copilot.memory.models  # noqa: F401  (registers tables on Base.metadata)
    from copilot.memory.db import Base

    engine = sa.create_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(engine)
    engine.dispose()
    yield
    _clear_db_caches()


class _CountingEmbedder:
    """A real (stub) embedder that also counts how many times it was called.

    Used to pin F4: the embedder egress leg must be SKIPPED when the distilled
    query is empty, so a pure-name query never wastes a remote embed call.
    """

    def __init__(self) -> None:
        self.calls = 0
        self._inner = StubEmbedder()

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls += 1
        return self._inner.embed(texts)


async def _seed() -> None:
    from copilot.memory.db import session_scope
    from copilot.memory.repository import MemoryRepository

    stub = StubEmbedder()
    async with session_scope() as session:
        repo = MemoryRepository(session)
        doc = await repo.create_guideline_document(
            title="No-match test guideline", source="test:no-match", license="CC-BY-4.0"
        )
        for index, (section, content) in enumerate(_CORPUS):
            await repo.create_guideline_chunk(
                guideline_document_id=doc.id,
                content=content,
                section=section,
                chunk_index=index,
                embedding=stub.embed([content])[0],
            )


async def test_off_corpus_clinical_query_returns_no_evidence(rag_db: None) -> None:
    """A clinical question the corpus is silent on yields ``[]`` — not four cites.

    Pre-fix ``_dense_rank`` returned all four chunks at cosine 0.0, so this came
    back with a full, unrelated evidence block (RED). The corpus is NON-empty, so
    the only honest answer is no-evidence.
    """
    await _seed()
    retriever = build_retriever(get_settings())

    result = await retriever.retrieve(OFF_CORPUS_CLINICAL_QUERY, top_k=4)

    assert result == [], (
        "an off-corpus clinical query must return no evidence, not fabricated cites; "
        f"got {[e.section for e in result]!r}"
    )


async def test_pure_name_query_returns_no_evidence(rag_db: None) -> None:
    """A bare-name query (distills to "") yields ``[]``, never id-ordered cites.

    Pre-fix the empty distilled string embedded to the zero vector, cosine 0.0
    against every chunk, and ``_dense_rank`` handed back every id in id-order —
    so ``retrieve()`` served the first four corpus chunks to a question that is
    only a patient's name (RED).
    """
    await _seed()
    retriever = build_retriever(get_settings())

    result = await retriever.retrieve(PURE_NAME_QUERY, top_k=4)

    assert result == [], (
        "a pure-name query must return no evidence, not the first N corpus chunks; "
        f"got {[e.section for e in result]!r}"
    )


async def test_pure_name_query_skips_the_embedder_egress(rag_db: None) -> None:
    """F4: with an empty distilled query the embedder call is skipped entirely.

    On the keyed path an empty ``input`` is a wasted Voyage 400; on the keyless
    path it is a zero vector that contributes nothing. Either way the embed call
    is pointless, so it must not be made. Pre-fix it was (``calls == 1``, RED).
    """
    await _seed()
    embedder = _CountingEmbedder()
    retriever = build_retriever(get_settings(), embedder=embedder)

    result = await retriever.retrieve(PURE_NAME_QUERY, top_k=4)

    assert embedder.calls == 0, (
        "the embedder egress leg must be skipped when the distilled query is empty; "
        f"it was called {embedder.calls} time(s)"
    )
    assert result == [], "a pure-name query must still return no evidence"


async def test_in_corpus_query_still_returns_its_section(rag_db: None) -> None:
    """Positive control: the zero-drop must not over-reach.

    A genuinely matching query still retrieves its chunk on both legs, so the
    fix closes the no-match hole without suppressing real evidence.
    """
    await _seed()
    retriever = build_retriever(get_settings())

    result = await retriever.retrieve(IN_CORPUS_QUERY, top_k=4)

    assert result, "an in-corpus query must still return evidence after the zero-drop"
    assert result[0].section == "initial-resuscitation", (
        f"the matching section must lead the evidence block, got {[e.section for e in result]!r}"
    )
    assert result[0].score > 0.0
