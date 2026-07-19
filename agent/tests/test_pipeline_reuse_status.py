"""FIX 1 bite-proof: the dedupe REUSE path must not corrupt a good doc's status.

``attach_and_extract`` runs ``_extract`` on the reuse path too, against the
already-``extracted`` reused document. Before the fix, ``_extract``'s error
handler marked ANY document ``failed`` on a vision error — so a transient
re-extract failure on re-ingest downgraded the prior-good document to
``failed``. Because ``_find_reusable_document`` excludes ``failed``, the next
identical-bytes ingest then minted a DUPLICATE ``source_document`` row.

The fix threads a ``reused`` flag so the ``failed`` downgrade fires only for a
genuinely NEW ``document_id``; on reuse the error propagates without touching
status, and the prior ``extracted`` state (and its dedupe reuse) still stands.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from copilot.config import Settings
from copilot.documents.ocr import OcrToken
from copilot.documents.pipeline import DerivedOnlyUploader, DocumentIngestionService
from copilot.documents.vision import ExtractionResult
from copilot.domain.documents import ExtractedFact, LabReport
from copilot.domain.primitives import PatientId
from copilot.observability import NoopObservability

pytestmark = pytest.mark.asyncio

_PATIENT = PatientId(value=1015)


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
    db_file = tmp_path / "reuse_status.db"
    monkeypatch.setenv("COPILOT_DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
    _clear_db_caches()

    import copilot.memory.models  # noqa: F401  (registers tables on Base.metadata)
    from copilot.memory.db import Base

    engine = sa.create_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(engine)
    engine.dispose()
    yield db_file
    _clear_db_caches()


# --- deterministic, offline collaborators -----------------------------------


class _StubOcr:
    def recognize(
        self,
        image: bytes,
        page_no: int = 0,
        width: int | None = None,
        height: int | None = None,
    ) -> list[OcrToken]:
        return [
            OcrToken(text="Hemoglobin", bbox=[0.10, 0.10, 0.20, 0.03], conf=0.98),
            OcrToken(text="13.5", bbox=[0.32, 0.10, 0.06, 0.03], conf=0.97),
        ]


class _ToggleVision:
    """Succeeds unless ``raise_on_next`` is set — models a transient re-ingest failure."""

    model_name = "stub-vision-1"

    def __init__(self) -> None:
        self.raise_on_next = False
        self.calls = 0

    async def extract(self, pages: Sequence[Any], doc_type: Any) -> ExtractionResult:
        self.calls += 1
        if self.raise_on_next:
            raise RuntimeError("transient vision failure on re-ingest")
        return LabReport(
            facts=[
                ExtractedFact(field_path="hemoglobin", value="13.5", unit="g/dL", page_no=1)
            ]
        )


def _service(vision: _ToggleVision) -> DocumentIngestionService:
    return DocumentIngestionService(
        Settings(database_url="sqlite+aiosqlite:///:memory:"),
        write_client_factory=DerivedOnlyUploader,
        ocr=_StubOcr(),
        vision=vision,
        observability=NoopObservability(),
    )


def _fixture_pdf(text: str = "Hemoglobin 13.5 g/dL") -> bytes:
    """A minimal, deterministic, valid single-page PDF (rasterizable)."""
    objs: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [4 0 R] /Count 1 >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 3 0 R >> >> /Contents 5 0 R >>",
    ]
    stream = f"BT /F1 18 Tf 72 720 Td ({text}) Tj ET".encode()
    objs.append(
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream"
    )
    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_pos = len(out)
    out += f"xref\n0 {len(objs) + 1}\n".encode() + b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n"
    ).encode()
    return bytes(out)


# --- DB probes --------------------------------------------------------------


async def _status_of(document_id: int) -> str:
    from copilot.memory.db import session_scope
    from copilot.memory.repository import MemoryRepository

    async with session_scope() as session:
        row = await MemoryRepository(session).get_source_document(document_id)
        assert row is not None
        return row.status


async def _source_doc_count(patient_id: int) -> int:
    from copilot.memory.db import session_scope
    from copilot.memory.models import SourceDocumentRow

    async with session_scope() as session:
        result = await session.execute(
            sa.select(sa.func.count())
            .select_from(SourceDocumentRow)
            .where(SourceDocumentRow.patient_id == patient_id)
        )
        return int(result.scalar_one())


# --- the bite ---------------------------------------------------------------


async def test_reuse_extract_failure_does_not_downgrade_or_duplicate(agent_db: Path) -> None:
    pdf = _fixture_pdf()
    vision = _ToggleVision()
    service = _service(vision)

    # 1) First ingest of these bytes -> extracted, brand-new document.
    first = await service.attach_and_extract(
        patient_id=_PATIENT, content=pdf, doc_type="lab_pdf", correlation_id="c1"
    )
    assert first.status.value == "extracted"
    assert first.reused_upload is False
    assert await _status_of(first.source_document_id) == "extracted"

    # 2) Second ingest hits the REUSE path; vision raises (transient failure).
    vision.raise_on_next = True
    with pytest.raises(RuntimeError):
        await service.attach_and_extract(
            patient_id=_PATIENT, content=pdf, doc_type="lab_pdf", correlation_id="c2"
        )
    # The reused doc's prior successful extraction still stands — NOT downgraded.
    assert await _status_of(first.source_document_id) == "extracted", (
        "a transient re-extract failure on the REUSE path downgraded a good "
        "document to failed — the FIX 1 data-integrity defect"
    )

    # 3) Third identical ingest reuses the SAME doc (no duplicate row minted).
    vision.raise_on_next = False
    third = await service.attach_and_extract(
        patient_id=_PATIENT, content=pdf, doc_type="lab_pdf", correlation_id="c3"
    )
    assert third.reused_upload is True
    assert third.source_document_id == first.source_document_id
    assert await _source_doc_count(_PATIENT.value) == 1, (
        "the downgrade evicted the reusable doc, so a duplicate source_document "
        "row was minted for identical bytes"
    )
