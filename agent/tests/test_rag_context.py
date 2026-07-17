"""Contextual-retrieval upgrades: query expansion + heading-aware chunking.

Proves, without touching the frozen ``feat_rag`` acceptance suite:

1. Deterministic clinical-abbreviation expansion (abbreviation -> full term,
   append-only, deduped, and — critically — a no-scrub operation that must run
   only AFTER the PHI choke point).
2. Heading-aware chunking that preserves section metadata, nests a heading
   breadcrumb, and carries a bounded overlap across split boundaries.
3. The end-to-end retriever order (deidentify -> expand -> retrieve) keeps every
   outbound embedder/reranker payload PHI-free while enriching recall.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest
import sqlalchemy as sa

from copilot.config import get_settings
from copilot.rag.embeddings import StubEmbedder
from copilot.rag.ingest import chunk_body
from copilot.rag.query import CLINICAL_ABBREVIATIONS, expand_query
from copilot.rag.retriever import build_retriever

# --- query expansion (pure, no DB) ------------------------------------------


def test_expand_query_expands_common_abbreviations() -> None:
    assert "diabetic ketoacidosis" in expand_query("DKA management").lower()
    assert "acute kidney injury" in expand_query("workup for AKI").lower()
    assert "hypertension" in expand_query("HTN control").lower()
    assert "chronic kidney disease" in expand_query("CKD staging").lower()
    # Sx / Dx / Rx family (task-specified).
    assert "symptoms" in expand_query("presenting Sx").lower()
    assert "diagnosis" in expand_query("differential Dx").lower()
    assert "treatment" in expand_query("first-line Rx").lower()
    # Case-insensitive match on a digit-bearing key.
    assert "sodium-glucose cotransporter 2" in expand_query("euglycemic dka on sglt2").lower()


def test_expand_query_is_append_only_and_deterministic() -> None:
    query = "AKI and DKA on the floor"
    out = expand_query(query)
    # Original text preserved verbatim; expansions appended in appearance order.
    assert out == f"{query} acute kidney injury diabetic ketoacidosis"
    assert out.startswith(query)
    assert out == expand_query(query)  # byte-stable across calls


def test_expand_query_skips_when_full_term_already_present() -> None:
    query = "diabetic ketoacidosis (DKA) insulin infusion"
    # The full term is already in the query, so nothing is appended (no dup).
    assert expand_query(query) == query


def test_expand_query_leaves_non_clinical_and_empty_text_untouched() -> None:
    prose = "review the ward round schedule for tomorrow"
    assert expand_query(prose) == prose
    assert expand_query("   ") == ""


def test_clinical_lexicon_avoids_common_english_words() -> None:
    # Guards the append-only safety property: no key is an ordinary English word
    # that could mangle prose (e.g. "map", "ace"). Sanity-check the guards.
    for collider in ("map", "ace", "the", "for", "and"):
        assert collider not in CLINICAL_ABBREVIATIONS


# --- heading-aware chunking (pure, no DB) -----------------------------------


def test_chunking_preserves_flat_sections() -> None:
    body = (
        "\n## Insulin therapy\n\nStart an intravenous insulin infusion.\n\n"
        "## Potassium management\n\nReplete potassium before insulin.\n"
    )
    chunks = chunk_body(body)
    assert [c.section for c in chunks] == ["insulin-therapy", "potassium-management"]
    assert "insulin infusion" in chunks[0].content
    assert "Replete potassium" in chunks[1].content
    assert chunk_body(body) == chunks  # deterministic


def test_chunking_captures_preamble_and_nested_heading_breadcrumb() -> None:
    body = (
        "Intro paragraph before any heading.\n\n"
        "# Diabetic ketoacidosis\n\nOverview line.\n\n"
        "## Fluid therapy\n\nGive isotonic crystalloid.\n\n"
        "### Rates\n\nAbout one liter in the first hour.\n"
    )
    by_section = {c.section: c.content for c in chunk_body(body)}
    assert "preamble" in by_section
    assert "diabetic-ketoacidosis" in by_section
    assert "diabetic-ketoacidosis/fluid-therapy" in by_section
    assert "diabetic-ketoacidosis/fluid-therapy/rates" in by_section
    # The ### subsection is its own chunk, not buried inside its parent.
    assert "one liter" in by_section["diabetic-ketoacidosis/fluid-therapy/rates"]
    assert "one liter" not in by_section["diabetic-ketoacidosis/fluid-therapy"]


def test_chunking_carries_overlap_across_split_boundaries() -> None:
    body = "## Section one\n\nAlpha beta gamma delta.\n\nEpsilon zeta eta theta.\n"
    chunks = chunk_body(body, max_chars=30, overlap_chars=15)
    assert len(chunks) == 2
    assert all(c.section == "section-one" for c in chunks)
    assert "Alpha" in chunks[0].content and "delta" in chunks[0].content
    # The second piece repeats the tail of the first (overlap) plus its own text.
    assert "delta" in chunks[1].content
    assert "Epsilon" in chunks[1].content


# --- end-to-end: deidentify -> expand -> retrieve ---------------------------


def _clear_db_caches() -> None:
    from copilot.memory.db import get_engine, get_session_factory

    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()


@pytest.fixture
def rag_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Temp-file SQLite DB with the guideline tables pre-created; caches cleared."""
    db_file = tmp_path / "rag_context.db"
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
    """Real (deterministic, keyless) stub vectors, plus a record of every text."""

    def __init__(self) -> None:
        self._stub = StubEmbedder()
        self.calls: list[list[str]] = []

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        items = [str(t) for t in texts]
        self.calls.append(items)
        return self._stub.embed(items)

    @property
    def captured(self) -> list[str]:
        return [text for call in self.calls for text in call]


class _RecordingReranker:
    """Identity ordering (preserves fused rank), records query + document texts."""

    def __init__(self) -> None:
        self.captured: list[str] = []

    def rerank(self, query: str, documents: Sequence[str]) -> list[str]:
        docs = [str(d) for d in documents]
        self.captured.append(str(query))
        self.captured.extend(docs)
        return docs


async def _seed(chunks: list[tuple[str, str]]) -> None:
    from copilot.memory.db import session_scope
    from copilot.memory.repository import MemoryRepository

    stub = StubEmbedder()
    async with session_scope() as session:
        repo = MemoryRepository(session)
        doc = await repo.create_guideline_document(
            title="Test guideline", source="test:ctx", license="CC-BY-4.0"
        )
        for index, (section, content) in enumerate(chunks):
            await repo.create_guideline_chunk(
                guideline_document_id=doc.id,
                content=content,
                section=section,
                chunk_index=index,
                embedding=stub.embed([content])[0],
            )


async def test_retrieve_deidentifies_before_expanding(rag_db: None) -> None:
    await _seed(
        [
            ("insulin-therapy", "Continuous intravenous insulin infusion for diabetic ketoacidosis."),
            ("nephrotoxin-stewardship", "Hold nephrotoxins in acute kidney injury; avoid NSAIDs."),
            ("lactate", "Remeasure lactate in sepsis and start antibiotics within one hour."),
        ]
    )
    phi_query = "Patient: Jane Roe, MRN 55443322, DOB 07/04/1980 — best Rx for DKA and AKI?"

    # Rationale for the invariant: expand_query does NOT scrub, so running it on
    # the RAW query leaks PHI. It must therefore run only AFTER deidentify().
    leaked = expand_query(phi_query).lower()
    assert "roe" in leaked and "55443322" in leaked

    embedder = _RecordingEmbedder()
    reranker = _RecordingReranker()
    retriever = build_retriever(get_settings(), embedder=embedder, reranker=reranker)
    results = await retriever.retrieve(phi_query, top_k=3)
    assert results, "retrieval over the seeded corpus must return evidence"

    outbound = embedder.captured + reranker.captured
    assert outbound, "the retriever must route the query through the injected doubles"
    for text in outbound:
        low = text.lower()
        digits = "".join(ch for ch in text if ch.isdigit())
        assert "jane" not in low and "roe" not in low, f"name leaked into egress: {text!r}"
        assert "55443322" not in digits and "07041980" not in digits, (
            f"identifier digits leaked into egress: {text!r}"
        )

    # deidentify() ran (PHI gone) AND expand_query() ran (abbreviations expanded).
    embedded = " ".join(embedder.captured).lower()
    assert "diabetic ketoacidosis" in embedded  # DKA
    assert "acute kidney injury" in embedded  # AKI
    assert "treatment" in embedded  # Rx
