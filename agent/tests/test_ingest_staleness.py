"""A corrected guideline must actually apply — and a stale one must not verify.

The bug these pin: ``ingest_corpus`` skipped a document on the front-matter
``source`` alone. ``source`` says *which* document a file is, never *which
version*. So the documented remedy for a wrong dose in the corpus — edit the
markdown, re-run the ingest — was a **silent no-op** that reported ``skipped
(already ingested)``, i.e. read as success, while retrieval kept serving the
superseded text.

Why that is worse than an ordinary cache bug, and why the last test here matters
most: the serve-time verifier re-materializes the quoted chunk from the *same*
stale row (``copilot.verification.serve._read_guideline_chunk``), so the stale
quote matches itself verbatim and the claim is served as **grounded**. The
staleness is self-consistent, so the verification gate — the product's core safety
mechanism — structurally cannot catch it. Nothing downstream can detect this; it
has to be caught at ingest.

Fixture bodies below are deliberately shaped like the real probe: a vitamin-K dose
corrected from a 10x overdose to the right one.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from copilot.rag.embeddings import Embedder, StubEmbedder
from copilot.rag.ingest import ingest_corpus

_STALE_DOSE = "50-100 mg"
_CORRECT_DOSE = "5-10 mg"


def _corpus_body(dose: str) -> str:
    return f"""---
title: Warfarin Reversal
source: test://warfarin
license: CC0
---

# Warfarin Reversal

## Major bleeding

Give 4F-PCC together with {dose} of intravenous vitamin K.
"""


@pytest.fixture
def _db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from copilot.config import get_settings
    from copilot.memory.db import Base, get_engine, get_session_factory

    db_file = tmp_path / "staleness.db"
    monkeypatch.setenv("COPILOT_DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()
    import copilot.memory.models  # noqa: F401

    engine = sa.create_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(engine)
    engine.dispose()
    yield
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()


@pytest.fixture
def _corpus_dir(tmp_path: Path) -> Path:
    """An isolated one-document corpus, writable mid-test (the operator's edit)."""
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "warfarin.md").write_text(_corpus_body(_STALE_DOSE))
    return corpus


class _CountingEmbedder:
    """A StubEmbedder that records how many texts it was asked to embed.

    Re-embedding is the cost the skip path exists to avoid, so "did we skip?" is
    asserted against work actually performed, not merely against a report field a
    buggy ingester could still populate correctly. Structurally an ``Embedder`` —
    that Protocol is what ``ingest_corpus`` accepts.
    """

    def __init__(self) -> None:
        self._inner = StubEmbedder()
        self.embedded = 0

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.embedded += len(texts)
        return self._inner.embed(texts)


async def _ingest(corpus_dir: Path, *, force: bool = False, embedder: Embedder | None = None) -> Any:
    from copilot.memory.db import session_scope

    async with session_scope() as session:
        return await ingest_corpus(
            session, embedder or StubEmbedder(), corpus_dir=corpus_dir, force=force
        )


async def _served_chunks(corpus_dir: Path) -> list[str]:
    """Chunk text as the SERVER would re-read it — the row, not the file.

    Deliberately goes through the repository rather than re-reading the markdown:
    the file is not what gets cited, the row is, and the row is what went stale.
    """
    from copilot.memory.db import session_scope
    from copilot.memory.repository import MemoryRepository

    async with session_scope() as session:
        repository = MemoryRepository(session)
        document = await repository.get_guideline_document_by_source("test://warfarin")
        assert document is not None, "the corpus document must be registered"
        chunks = await repository.get_guideline_chunks(document.id)
        return [chunk.content for chunk in chunks]


def _served_text(corpus_dir: Path) -> str:
    import anyio

    return "\n".join(anyio.run(_served_chunks, corpus_dir))


# --- the headline: an edit applies without --force ---------------------------------


def test_edited_document_is_reingested_without_force(_db: None, _corpus_dir: Path) -> None:
    """The exact probe from the field report, as a test.

    ingest v1 -> operator corrects the dose in the markdown -> re-ingest with NO
    flags -> the SERVED chunk must be the new text. Before the fix this served
    ``50-100 mg`` and reported success.
    """
    import anyio

    anyio.run(_ingest, _corpus_dir)
    assert _STALE_DOSE in _served_text(_corpus_dir), "v1 must ingest the original text"

    # The operator spots a 10x vitamin-K overdose and fixes the corpus file.
    (_corpus_dir / "warfarin.md").write_text(_corpus_body(_CORRECT_DOSE))

    report = anyio.run(_ingest, _corpus_dir)

    served = _served_text(_corpus_dir)
    assert _CORRECT_DOSE in served, "a corrected guideline MUST apply on a plain re-ingest"
    assert _STALE_DOSE not in served, "the superseded dose must not survive anywhere"
    # The report must not claim success by calling this a skip.
    assert report.documents_skipped == 0
    assert report.documents_ingested == 1
    assert report.results[0].reason == "changed"


def test_reingest_replaces_rather_than_accumulates(_db: None, _corpus_dir: Path) -> None:
    """The old chunks must be GONE, not merely outnumbered.

    A rebuild that appended would leave the stale dose retrievable and citable —
    and, because the verifier re-reads whatever row a citation names, still
    "grounded".
    """
    import anyio

    anyio.run(_ingest, _corpus_dir)
    (_corpus_dir / "warfarin.md").write_text(_corpus_body(_CORRECT_DOSE))
    anyio.run(_ingest, _corpus_dir)

    async def _count() -> int:
        from copilot.memory.db import session_scope
        from copilot.memory.repository import MemoryRepository

        async with session_scope() as session:
            return len(await MemoryRepository(session).list_guideline_chunks())

    assert anyio.run(_count) == 1, "one document, one section — a rebuild must replace"


def test_stale_text_cannot_be_served_as_grounded_after_correction(
    _db: None, _corpus_dir: Path
) -> None:
    """Why this bug outranks a cache miss: verification cannot catch it.

    The verifier re-materializes a cited chunk from the stored row, so a stale row
    grounds its own stale quote. This asserts the property the verifier depends on
    but cannot itself establish: after a corpus correction + plain re-ingest, no
    row exists that could ground the retracted dose.
    """
    import anyio

    anyio.run(_ingest, _corpus_dir)
    (_corpus_dir / "warfarin.md").write_text(_corpus_body(_CORRECT_DOSE))
    anyio.run(_ingest, _corpus_dir)

    async def _quote_verifies(quote: str) -> bool:
        """Does ANY stored chunk contain this quote verbatim? (what serve.py asks)"""
        from copilot.memory.db import session_scope
        from copilot.memory.repository import MemoryRepository

        async with session_scope() as session:
            chunks = await MemoryRepository(session).list_guideline_chunks()
            return any(quote in chunk.content for chunk in chunks)

    stale_claim = f"Give 4F-PCC together with {_STALE_DOSE} of intravenous vitamin K."
    correct_claim = f"Give 4F-PCC together with {_CORRECT_DOSE} of intravenous vitamin K."
    assert not anyio.run(_quote_verifies, stale_claim), (
        "the retracted dose must have NO grounding row left — otherwise the verifier "
        "re-reads it and serves a 10x overdose as verified"
    )
    assert anyio.run(_quote_verifies, correct_claim), "the corrected dose must be groundable"


# --- the regression guard: unchanged stays cheap ----------------------------------


def test_unchanged_document_is_skipped_and_not_re_embedded(_db: None, _corpus_dir: Path) -> None:
    """Without this, "always re-ingest" would pass the headline test for the wrong reason.

    Asserts against embedding work performed, not just the report: re-embedding the
    whole corpus on every boot is the failure mode a naive fix introduces.
    """
    import anyio

    first = _CountingEmbedder()
    anyio.run(lambda: _ingest(_corpus_dir, embedder=first))
    assert first.embedded > 0, "the first ingest must embed"

    second = _CountingEmbedder()
    report = anyio.run(lambda: _ingest(_corpus_dir, embedder=second))

    assert second.embedded == 0, "an unchanged document must not be re-embedded"
    assert report.documents_ingested == 0
    assert report.documents_skipped == 1
    assert report.results[0].reason == "unchanged"
    assert report.results[0].skipped is True


def test_unchanged_reingest_leaves_the_row_identical(_db: None, _corpus_dir: Path) -> None:
    """A skip must be a true no-op — same row id, same ingested_at, same hash."""
    import anyio

    async def _snapshot() -> tuple[int, Any, str | None]:
        from copilot.memory.db import session_scope
        from copilot.memory.repository import MemoryRepository

        async with session_scope() as session:
            row = await MemoryRepository(session).get_guideline_document_by_source(
                "test://warfarin"
            )
            assert row is not None
            return row.id, row.ingested_at, row.content_hash

    anyio.run(_ingest, _corpus_dir)
    before = anyio.run(_snapshot)
    anyio.run(_ingest, _corpus_dir)
    assert anyio.run(_snapshot) == before, "an unchanged skip must not rewrite the row"


def test_ingest_records_a_content_hash(_db: None, _corpus_dir: Path) -> None:
    """The hash must actually land — a NULL here would re-arm the bug every run."""
    import anyio

    anyio.run(_ingest, _corpus_dir)

    async def _hash() -> str | None:
        from copilot.memory.db import session_scope
        from copilot.memory.repository import MemoryRepository

        async with session_scope() as session:
            row = await MemoryRepository(session).get_guideline_document_by_source(
                "test://warfarin"
            )
            assert row is not None
            return row.content_hash

    stored = anyio.run(_hash)
    assert stored is not None and len(stored) == 64, "a sha256 hex digest must be recorded"


# --- pre-migration rows: NULL hash means UNKNOWN, so refresh ----------------------


def test_null_hash_row_is_reingested_and_backfilled(_db: None, _corpus_dir: Path) -> None:
    """A row written before migration 0009 has no hash: rebuild it once.

    NULL means *unknown*, not *unchanged* — nothing on the row can establish that it
    matches the file. Treating it as current would preserve this exact bug for every
    already-deployed corpus (the population most likely to be holding stale text).
    Simulated the only honest way: NULL the hash, as the migration leaves it.
    """
    import anyio

    anyio.run(_ingest, _corpus_dir)

    async def _null_the_hash() -> None:
        from copilot.memory.db import session_scope

        async with session_scope() as session:
            await session.execute(sa.text("UPDATE guideline_document SET content_hash = NULL"))

    anyio.run(_null_the_hash)

    embedder = _CountingEmbedder()
    report = anyio.run(lambda: _ingest(_corpus_dir, embedder=embedder))

    assert report.documents_skipped == 0, "an unknown-freshness row must not be trusted"
    assert report.results[0].reason == "unknown-hash"
    assert embedder.embedded > 0, "the pre-migration row must actually be rebuilt"

    # ...and it self-heals: the rebuild records a hash, so the NEXT run is cheap again.
    after = _CountingEmbedder()
    second = anyio.run(lambda: _ingest(_corpus_dir, embedder=after))
    assert after.embedded == 0, "the backfilled hash must restore the cheap skip path"
    assert second.documents_skipped == 1


# --- --force keeps working --------------------------------------------------------


def test_force_reingests_unchanged_document_unconditionally(_db: None, _corpus_dir: Path) -> None:
    """--force must ignore a matching hash — it exists for embedder changes, which
    no content hash can detect."""
    import anyio

    anyio.run(_ingest, _corpus_dir)

    embedder = _CountingEmbedder()
    report = anyio.run(lambda: _ingest(_corpus_dir, force=True, embedder=embedder))

    assert report.documents_skipped == 0, "--force must skip nothing"
    assert report.documents_ingested == 1
    assert report.results[0].reason == "forced"
    assert embedder.embedded > 0, "--force must re-embed even when the text is identical"
