#!/usr/bin/env python3
"""Reproducible, idempotent ingest of the in-repo guideline corpus.

Chunks every front-mattered Markdown source under ``agent/corpus/``
(per-source ``title``/``source``/``license`` metadata — see
``agent/corpus/LICENSES.md``) and persists ``guideline_document`` /
``guideline_chunk`` rows, embeddings included, into the agent-owned database
(``COPILOT_DATABASE_URL``).

Offline/CI-safe by default: with no ``COPILOT_VOYAGE_API_KEY`` set,
``build_embedder`` selects the deterministic keyless Voyage stub, so the run
makes zero network calls and produces byte-identical vectors every time.

Idempotent by *content*: each source is skipped only when its stored
``content_hash`` still matches the file, so re-running never duplicates documents
or chunks — but **editing a corpus file and re-running does apply the edit**, with
no flag required. Correcting a guideline is the case that must never silently
no-op (see ``copilot.rag.ingest``).

Usage (from the ``agent/`` directory)::

    python scripts/ingest_guidelines.py [--corpus-dir PATH] [--force]

For SQLite targets the schema is created on the fly (test/dev convenience);
Postgres deployments must have Alembic migrations applied first — the script
never bypasses them.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

# Make ``import copilot`` resolve to THIS checkout's package even when the
# interpreter has no copilot install: invoked by path, sys.path[0] is
# ``scripts/`` — not the ``agent/`` dir that contains the package.
_AGENT_DIR = Path(__file__).resolve().parents[1]
if str(_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENT_DIR))


async def _run(corpus_dir: Path | None, force: bool) -> None:
    import copilot.memory.models  # noqa: F401  (registers every table on Base.metadata)
    from copilot.config import get_settings
    from copilot.memory.db import Base, get_engine, session_scope
    from copilot.rag.embeddings import build_embedder
    from copilot.rag.ingest import ingest_corpus

    settings = get_settings()
    try:
        if settings.database_url.startswith("sqlite"):
            # Test/dev convenience only — the Postgres schema is Alembic-owned.
            async with get_engine().begin() as conn:
                await conn.run_sync(Base.metadata.create_all)

        embedder = build_embedder(settings)
        # session_scope commits on success and rolls back on error, so a failed
        # run never leaves a half-ingested document behind.
        async with session_scope() as session:
            report = await ingest_corpus(
                session, embedder, corpus_dir=corpus_dir, force=force
            )
    finally:
        await get_engine().dispose()

    print("=== AgentForge guideline-corpus ingest ===")
    for result in report.results:
        # ``result.label`` says WHY, not just whether — the old report printed
        # "skipped (already ingested)" for a corpus file that had been corrected
        # and not applied, so the one line an operator reads asserted success at
        # the exact moment the ingest was serving superseded clinical text.
        state = result.label if result.skipped else f"{result.label} — {result.chunk_count} chunks"
        print(f"- {result.title} [{result.source}]: {state}")
    print(
        f"documents ingested: {report.documents_ingested}  "
        f"skipped: {report.documents_skipped}  "
        f"chunks ingested: {report.chunks_ingested}"
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(
        description="Reproducible, idempotent ingest of the in-repo guideline corpus."
    )
    parser.add_argument(
        "--corpus-dir",
        type=Path,
        default=None,
        help="Corpus directory to ingest (default: agent/corpus/).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Delete every discovered source's existing rows and re-ingest, changed "
            "or not. NOT needed for an edited corpus file — the default already "
            "detects that by content hash. REQUIRED after an EMBEDDER change: "
            "stored vectors are incomparable with queries embedded by a different "
            "embedder, and that degradation lives in the embedding, not the text, "
            "so no content hash can see it. Safe — the corpus is reproducible from "
            "the repo."
        ),
    )
    args = parser.parse_args()
    asyncio.run(_run(args.corpus_dir, args.force))


if __name__ == "__main__":
    main()
