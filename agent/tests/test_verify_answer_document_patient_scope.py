"""FIX 2 bite-proof: serve-time document-fact grounding is patient-scoped.

``verify_answer`` re-materializes a ``DocumentCitation`` via
``_read_document_fact`` -> ``MemoryRepository.get_extracted_fact_by_id``. Before
the fix that lookup bound a fact to its ``source_document`` but NOT to the turn's
patient, so a patient-A turn citing patient B's ``(source_document, fact)`` id
pair could ground against B's fact — a cross-patient leak.

The fix threads ``patient_id`` through to the repository, whose query now also
joins ``source_document`` and filters on ``patient_id`` — mirroring the intake
extractor's ``document.patient_id == patient_id`` boundary. A cross-patient fact
fails closed to ``None`` (claim dropped); the same-patient case still resolves.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from copilot.domain.contracts import Claim, VerificationAction
from copilot.domain.primitives import DocumentCitation, PatientId, ResourceType
from copilot.verification.serve import verify_answer

pytestmark = pytest.mark.asyncio

_PATIENT_A = PatientId(value=1015)
_PATIENT_B = PatientId(value=2020)


# --- DB fixture -------------------------------------------------------------


def _clear_db_caches() -> None:
    from copilot.config import get_settings
    from copilot.memory.db import get_engine, get_session_factory

    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()


@pytest.fixture
def agent_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Temp-file SQLite DB with every agent table created; caches cleared."""
    db_file = tmp_path / "doc_patient_scope.db"
    monkeypatch.setenv("COPILOT_DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
    _clear_db_caches()

    import copilot.memory.models  # noqa: F401  (registers tables on Base.metadata)
    from copilot.memory.db import Base

    engine = sa.create_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(engine)
    engine.dispose()
    yield db_file
    _clear_db_caches()


# --- helpers ----------------------------------------------------------------


class _NoFhir:
    """A FHIR reader that must never be called — these claims are document-cited."""

    async def read(self, resource_type: ResourceType, resource_id: str) -> dict[str, Any]:
        raise AssertionError("no FHIR read expected for a document-cited claim")


async def _seed_document_fact(patient_id: int, value: str) -> tuple[int, int]:
    """Seed one extracted, supported, well-grounded document fact for a patient.

    Returns ``(source_document_id, extracted_fact_id)``.
    """
    from copilot.memory.db import session_scope
    from copilot.memory.repository import MemoryRepository

    async with session_scope() as session:
        repo = MemoryRepository(session)
        doc = await repo.create_source_document(
            patient_id=patient_id,
            doc_type="lab_pdf",
            correlation_id="seed",
            openemr_document_id=f"oe-{patient_id}",
            content_hash=f"hash-{patient_id}",
            status="extracted",
        )
        extraction = await repo.create_extraction(
            source_document_id=doc.id, correlation_id="seed", status="ok"
        )
        fact = await repo.create_extracted_fact(
            extraction_id=extraction.id,
            field_path="hemoglobin",
            value=value,
            page_no=1,
            bbox=[0.10, 0.10, 0.10, 0.10],
            match_confidence=0.90,  # >= doc_grounding_confidence_threshold (0.5)
            supported=True,
        )
        return doc.id, fact.id


def _doc_claim(source_id: int, fact_id: int, value: str = "13.5") -> Claim:
    return Claim(
        text=f"Hemoglobin {value} g/dL.",
        source_ref=DocumentCitation(
            source_id=str(source_id),
            page_or_section=1,
            field_or_chunk_id=str(fact_id),
            quote_or_value=value,
        ),
    )


# --- the bite ---------------------------------------------------------------


async def test_cross_patient_document_fact_is_not_grounded(agent_db: Path) -> None:
    """A patient-A turn citing patient B's document fact drops the claim (withheld)."""
    await _seed_document_fact(_PATIENT_A.value, "13.5")
    doc_b, fact_b = await _seed_document_fact(_PATIENT_B.value, "13.5")

    # Patient-A turn, but the claim cites patient B's (document, fact) id pair.
    claim = _doc_claim(doc_b, fact_b)
    result = await verify_answer([claim], _PATIENT_A, _NoFhir())

    assert result.action == VerificationAction.withheld, (
        "a patient-A turn grounded a claim against patient B's document fact — "
        "the FIX 2 cross-patient leak"
    )
    assert result.claims[0].attribution_ok is False


async def test_same_patient_document_fact_still_grounds(agent_db: Path) -> None:
    """The same-patient case is unaffected: a well-grounded fact still serves."""
    doc_a, fact_a = await _seed_document_fact(_PATIENT_A.value, "13.5")

    claim = _doc_claim(doc_a, fact_a)
    result = await verify_answer([claim], _PATIENT_A, _NoFhir())

    assert result.action == VerificationAction.served
    assert result.claims[0].attribution_ok is True
    assert result.claims[0].value_match is True
