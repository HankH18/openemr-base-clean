"""Patient-scoping on the graph intake-extractor read path (P1, cross-patient IDOR).

Round-2 audit found a live cross-patient PHI leak. ``POST /v1/chat`` authorizes
only ``req.patient_id``; ``req.document_ids`` ride into the graph UNAUTHORIZED. In
graph mode the supervisor routes a task with non-empty ``document_ids`` to the
intake-extractor, whose ``_read_extractions`` loaded each document's stored facts
filtered by *document id only* — no patient scoping. So a clinician on patient A's
turn could name patient B's ``document_id`` and read B's supported facts, which
then egress to the model as A's turn and are absent from B's audit.

Guarded here at the worker's public entry (``DocumentIntakeExtractor.run`` over a
real :class:`AgentTask`): a task scoped to patient A surfaces NONE of patient B's
facts, even when B's document id is named in the task. The fix scopes each id to
its source document's ``patient_id`` — the same boundary the document read route
enforces with ``is_authorized(cid, doc.patient_id)``.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import anyio
import pytest
import sqlalchemy as sa

_PATIENT_A = 111
_PATIENT_B = 222


@pytest.fixture
def _db_file(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Point Settings at a temp SQLite file and create the schema.

    ``_read_extractions`` opens its own ``session_scope()`` off
    ``COPILOT_DATABASE_URL``, so the scenario and the worker share this file.
    """
    from copilot.config import get_settings
    from copilot.memory.db import Base, get_engine, get_session_factory

    db_file = tmp_path / "intake_scope.db"
    monkeypatch.setenv("COPILOT_DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
    monkeypatch.setenv("COPILOT_FHIR_BASE_URL", "http://oe.test/fhir")
    monkeypatch.setenv("COPILOT_ANTHROPIC_API_KEY", "")
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()

    import copilot.memory.models  # noqa: F401  (registers tables on Base.metadata)

    sync_engine = sa.create_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()
    yield str(db_file)
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()


async def _seed_document_with_fact(patient_id: int, field_path: str, value: str) -> int:
    """Insert one extracted, supported document fact for a patient; return the doc id."""
    from copilot.memory.db import session_scope
    from copilot.memory.repository import MemoryRepository

    async with session_scope() as session:
        repo = MemoryRepository(session)
        doc = await repo.create_source_document(
            patient_id=patient_id,
            doc_type="lab_pdf",
            correlation_id="c-scope",
            openemr_document_id=f"oe-{patient_id}",
            content_hash=f"hash-{patient_id}",
            page_count=1,
            status="extracted",
        )
        extraction = await repo.create_extraction(
            source_document_id=doc.id,
            correlation_id="c-scope",
            schema_version="w2-v1",
            model="stub",
            confidence_overall=0.9,
            status="ok",
        )
        await repo.create_extracted_fact(
            extraction_id=extraction.id,
            field_path=field_path,
            value=value,
            supported=True,
        )
        return int(doc.id)


async def _run_intake(patient_id: int, document_ids: list[str]) -> Any:
    """Run the intake-extractor worker over a real task (its actual serve entry)."""
    from copilot.config import Settings
    from copilot.graph.contracts import AgentTask
    from copilot.graph.intake_extractor import DocumentIntakeExtractor

    task = AgentTask(
        patient_id=patient_id,
        question="What do the attached results show?",
        document_ids=document_ids,
    )
    return await DocumentIntakeExtractor(Settings(anthropic_api_key="")).run(task)


def test_intake_does_not_leak_another_patients_facts(_db_file: str) -> None:
    """A task scoped to patient A naming patient B's document id surfaces NONE of
    B's facts. RED before the P1 fix (B's supported facts leaked into A's turn),
    GREEN after (the foreign document is skipped before any fact is read)."""

    async def _scenario() -> Any:
        doc_b = await _seed_document_with_fact(_PATIENT_B, "potassium", "5.4")
        return await _run_intake(_PATIENT_A, [str(doc_b)])

    report = anyio.run(_scenario)

    assert report.fact_count == 0, "patient B's supported facts must not be counted for A"
    assert report.facts == [], "patient B's fact values must not be surfaced to A"
    assert report.extraction_confidence == 0.0, "B's extraction confidence must not bleed into A"


def test_intake_keeps_own_patient_facts_and_drops_the_foreign_doc(_db_file: str) -> None:
    """Positive control + mixed set: with both A's and B's document ids in scope on
    patient A's task, ONLY A's fact survives — the scope filters B without
    over-blocking A's own document."""

    async def _scenario() -> Any:
        doc_a = await _seed_document_with_fact(_PATIENT_A, "sodium", "140")
        doc_b = await _seed_document_with_fact(_PATIENT_B, "potassium", "5.4")
        return await _run_intake(_PATIENT_A, [str(doc_a), str(doc_b)])

    report = anyio.run(_scenario)

    assert report.fact_count == 1, "exactly patient A's one supported fact"
    values = {(fact.field_path, fact.value) for fact in report.facts}
    assert values == {("sodium", "140")}, f"only A's fact may surface, got {values}"
