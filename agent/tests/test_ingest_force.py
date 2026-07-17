"""`ingest_corpus(force=True)` must actually rebuild, not silently skip.

Guards the trap found when the keyless embedder changed: chunk vectors are
persisted at ingest, so vectors written by an old embedder are incomparable with
queries embedded by a new one — retrieval quietly degrades to noise. The natural
fix ("just re-ingest") was a SILENT NO-OP, because ingest skips any source already
registered. Without --force there is no supported way to rebuild the corpus, and
nothing tells the operator why retrieval got worse.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa

from copilot.rag.embeddings import StubEmbedder
from copilot.rag.ingest import ingest_corpus


@pytest.fixture
def _db(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from copilot.config import get_settings
    from copilot.memory.db import Base, get_engine, get_session_factory

    db_file = tmp_path / "ingest.db"
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


async def _ingest(force: bool) -> Any:
    from copilot.memory.db import session_scope

    async with session_scope() as session:
        return await ingest_corpus(session, StubEmbedder(), force=force)


def test_second_ingest_without_force_skips_everything(_db: None) -> None:
    import anyio

    first = anyio.run(_ingest, False)
    assert first.documents_ingested > 0, "the corpus must ingest on a clean DB"
    second = anyio.run(_ingest, False)
    # The trap, pinned: this is the no-op that makes an embedder change invisible.
    assert second.documents_ingested == 0
    assert second.documents_skipped == first.documents_ingested


def test_force_reingests_every_source(_db: None) -> None:
    import anyio

    first = anyio.run(_ingest, False)
    forced = anyio.run(_ingest, True)
    assert forced.documents_skipped == 0, "--force must skip nothing"
    assert forced.documents_ingested == first.documents_ingested
    assert forced.chunks_ingested == first.chunks_ingested


def test_force_does_not_duplicate_rows(_db: None) -> None:
    # Rebuild must REPLACE, not accumulate — a duplicated corpus would double-count
    # in retrieval and silently skew fusion.
    import anyio

    from copilot.memory.db import session_scope
    from copilot.memory.repository import MemoryRepository

    anyio.run(_ingest, False)
    anyio.run(_ingest, True)

    async def _count() -> int:
        async with session_scope() as session:
            return len(await MemoryRepository(session).list_guideline_chunks())

    after_force = anyio.run(_count)
    anyio.run(_ingest, True)
    assert anyio.run(_count) == after_force, "repeated --force must be idempotent"
