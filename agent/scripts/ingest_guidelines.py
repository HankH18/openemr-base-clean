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
Idempotent: a source whose front-matter ``source`` is already registered is
skipped, so re-running never duplicates documents or chunks.

Usage (from the ``agent/`` directory)::

    python scripts/ingest_guidelines.py [--corpus-dir PATH]

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


async def _run(corpus_dir: Path | None) -> None:
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
            report = await ingest_corpus(session, embedder, corpus_dir=corpus_dir)
    finally:
        await get_engine().dispose()

    print("=== AgentForge guideline-corpus ingest ===")
    for result in report.results:
        state = (
            "skipped (already ingested)"
            if result.skipped
            else f"ingested ({result.chunk_count} chunks)"
        )
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
    args = parser.parse_args()
    asyncio.run(_run(args.corpus_dir))


if __name__ == "__main__":
    main()
