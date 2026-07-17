"""The retriever must serve the section a clinician actually asked for.

Two queries were measured serving clinically WRONG guidance against the real corpus,
on the deployed keyless path (VOYAGE/COHERE unset, which is what DEPLOY.md instructs)::

    "How do I reverse warfarin in major life-threatening bleeding?"
        -> supratherapeutic-inr-without-bleeding    <- INR-HOLD ADVICE, FOR A MAJOR BLEED
    "Which nephrotoxins should I stop in AKI?"
        -> initial-evaluation

A hospitalist asks how to reverse warfarin in a patient who is exsanguinating and the
guideline block leads with advice for a patient who is NOT bleeding: hold the dose,
recheck the INR. The correct section exists in the corpus and was not served first.

This was not the reranker defect (fixed in 7bc9839) — both failed under an identity
rerank too. It was the sparse leg scoring a raw un-normalized term-frequency sum, with
no IDF: "warfarin" (in every section of that document) counted as much as "bleeding"
(rare, and the whole question). BM25 weights a term by how much it narrows the corpus,
so the decisive word wins.

These assert against the REAL corpus through the REAL retriever, because the defect was
invisible to every fixture-shaped test: the bug is in how real prose distributes terms.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

_CASES = [
    # (question, expected section substring)
    ("How do I reverse warfarin in major life-threatening bleeding?", "major-bleeding"),
    ("Which nephrotoxins should I stop in AKI?", "nephrotoxin"),
    # The three the eval gate also pins — duplicated here deliberately, so a change
    # that fixes the two above by breaking these fails HERE too, not only in the gate.
    ("What MAP should I target in septic shock?", "vasopressors-and-map-target"),
    ("How much crystalloid should I give for initial resuscitation in sepsis?", "initial-resuscitation"),
    ("What are the urgent indications for renal replacement therapy?", "indications-for-renal-replacement"),
]


@pytest.fixture
def _corpus_db() -> object:
    """Ingest the real corpus into a throwaway SQLite DB on the keyless path."""
    from copilot.config import get_settings
    from copilot.memory.db import get_engine, get_session_factory

    with tempfile.TemporaryDirectory() as td:
        prev = {k: os.environ.get(k) for k in ("COPILOT_DATABASE_URL", "COPILOT_VOYAGE_API_KEY", "COPILOT_COHERE_API_KEY")}
        os.environ["COPILOT_DATABASE_URL"] = f"sqlite+aiosqlite:///{Path(td) / 't.db'}"
        os.environ["COPILOT_VOYAGE_API_KEY"] = ""
        os.environ["COPILOT_COHERE_API_KEY"] = ""
        for cache in (get_settings, get_engine, get_session_factory):
            cache.cache_clear()
        yield None
        for k, v in prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        for cache in (get_settings, get_engine, get_session_factory):
            cache.cache_clear()


@pytest.mark.anyio
@pytest.mark.parametrize(("question", "expected"), _CASES)
async def test_the_clinician_gets_the_section_they_asked_for(
    _corpus_db: object, question: str, expected: str
) -> None:
    import copilot.memory.models  # noqa: F401  (registers tables)
    from copilot.config import get_settings
    from copilot.memory.db import Base, get_engine, session_scope
    from copilot.rag.embeddings import StubEmbedder
    from copilot.rag.ingest import ingest_corpus
    from copilot.rag.retriever import build_retriever

    async with get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with session_scope() as session:
        await ingest_corpus(session, StubEmbedder(), corpus_dir=Path("corpus"))

    hits = await build_retriever(get_settings()).retrieve(question, top_k=4)

    assert hits, f"retrieval returned NOTHING for {question!r} — the gate cannot grade silence"
    assert expected in hits[0].section, (
        f"{question!r}\n  served : {hits[0].section}\n  wanted : ~{expected}\n"
        "The first chunk is what leads the clinician's evidence block."
    )
