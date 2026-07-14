"""feat_rag criterion 1 — corpus + reproducible, idempotent ingest.

FROZEN GOALS. In-repo corpus files with license metadata are ingested by
``agent/scripts/ingest_guidelines.py`` (chunk + persist into guideline_document
/ guideline_chunk); re-running the script adds no duplicate chunks. Runs the
script as a black-box subprocess against the per-test SQLite DB (the env
fixture already exported COPILOT_DATABASE_URL and empty provider keys, so the
ingest must be keyless/offline-capable).
"""

from __future__ import annotations

import os
import subprocess
import sys

import sqlalchemy as sa
from sqlalchemy.orm import Session

import _rag_helpers as H


def _run_ingest(script) -> None:
    proc = subprocess.run(
        [sys.executable, str(script)],
        cwd=str(H.AGENT_DIR),
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=300,
    )
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "")[-800:]
        H.fail(f"ingest_guidelines.py exited {proc.returncode}:\n{tail}")


def _counts() -> tuple[int, int, list]:
    from copilot.memory.models import GuidelineChunkRow, GuidelineDocumentRow

    engine = sa.create_engine(H.sync_db_url())
    try:
        with Session(engine) as session:
            docs = list(session.scalars(sa.select(GuidelineDocumentRow)))
            n_chunks = session.scalar(sa.select(sa.func.count(GuidelineChunkRow.id))) or 0
            doc_meta = [(d.title, d.source, d.license) for d in docs]
            chunk_texts = [
                c.content for c in session.scalars(sa.select(GuidelineChunkRow))
            ]
    finally:
        engine.dispose()
    return len(doc_meta), int(n_chunks), [doc_meta, chunk_texts]


def test_rag_01_corpus_ingest_reproducible_and_idempotent():
    script = H.AGENT_DIR / "scripts" / "ingest_guidelines.py"
    if not script.is_file():
        H.fail(
            "agent/scripts/ingest_guidelines.py does not exist — the reproducible corpus "
            "ingest script is the pinned entry point for the guideline corpus"
        )

    _run_ingest(script)
    n_docs, n_chunks, (doc_meta, chunk_texts) = _counts()

    assert n_docs >= 2, (
        f"expected a small hospitalist corpus (>=2 guideline documents), found {n_docs}"
    )
    assert n_chunks >= 4, f"expected >=4 persisted guideline chunks, found {n_chunks}"
    for title, source, license_ in doc_meta:
        assert title and str(title).strip(), "every guideline document needs a title"
        assert source and str(source).strip(), (
            f"guideline document {title!r} is missing source metadata (in-repo provenance)"
        )
        assert license_ and str(license_).strip(), (
            f"guideline document {title!r} is missing license metadata"
        )
    assert all(t and t.strip() for t in chunk_texts), "chunks must carry non-empty text"

    # Idempotency: a re-run must not duplicate documents or chunks.
    _run_ingest(script)
    n_docs_2, n_chunks_2, _ = _counts()
    assert n_docs_2 == n_docs, (
        f"re-running the ingest duplicated documents: {n_docs} -> {n_docs_2}"
    )
    assert n_chunks_2 == n_chunks, (
        f"re-running the ingest duplicated chunks: {n_chunks} -> {n_chunks_2}"
    )
