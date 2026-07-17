"""feat_rag criterion 2 — embeddings behind a Protocol (Voyage Stub/Real).

FROZEN GOALS. With no ``voyage_api_key`` set, ``build_embedder(settings)``
must return the deterministic Stub: 1024-dim vectors, no network traffic
(asserted with a strict respx guard — any outbound httpx call fails the test),
identical vectors across calls, distinct vectors for distinct texts. The
persisted-vector path is exercised on SQLite via the JSON fallback column
(pgvector on Postgres / JSON list on SQLite — same list[float] semantics).
"""

from __future__ import annotations

import pytest
import respx
import sqlalchemy as sa
from sqlalchemy.orm import Session

import _rag_helpers as H


async def test_rag_02_embeddings_stub_1024d_deterministic_sqlite_fallback():
    embedder = H.build_embedder()

    texts = [
        "continuous intravenous insulin infusion for diabetic ketoacidosis",
        "remeasure lactate in sepsis and start antibiotics within one hour",
    ]

    # Zero network in tests: the keyless factory must have selected the Stub —
    # any outbound httpx request inside this block raises.
    with respx.mock:
        vecs = await H.embed_texts(embedder, texts)
        vecs_again = await H.embed_texts(embedder, texts)

    assert len(vecs) == 2, f"embed() must return one vector per input, got {len(vecs)}"
    for v in vecs:
        assert len(v) == H.EMBED_DIM, (
            f"voyage-3.5 embeddings are {H.EMBED_DIM}-dim; got {len(v)}"
        )
        assert all(isinstance(x, float) for x in v)
    assert vecs == vecs_again, "stub embeddings must be deterministic across calls"
    assert vecs[0] != vecs[1], "distinct texts must embed to distinct vectors"

    # pgvector-PG / JSON-SQLite fallback: a stub vector persists on SQLite and
    # round-trips as the same list[float] through the embedding column.
    from copilot.memory.models import GuidelineChunkRow, GuidelineDocumentRow

    engine = sa.create_engine(H.sync_db_url())
    try:
        with Session(engine) as session:
            doc = GuidelineDocumentRow(
                title="embedding fallback fixture",
                source="acceptance-fixture:fallback",
                license="CC-BY-4.0",
            )
            session.add(doc)
            session.flush()
            row = GuidelineChunkRow(
                guideline_document_id=doc.id,
                section="fallback",
                chunk_index=0,
                content=texts[0],
                embedding=vecs[0],
            )
            session.add(row)
            session.commit()
            chunk_id = row.id
        with Session(engine) as session:
            stored = session.get(GuidelineChunkRow, chunk_id)
            assert stored is not None
            loaded = [float(x) for x in stored.embedding]
    finally:
        engine.dispose()

    assert len(loaded) == H.EMBED_DIM
    assert loaded == pytest.approx(vecs[0]), (
        "the SQLite JSON fallback must round-trip the exact embedding values"
    )
