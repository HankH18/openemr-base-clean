"""Document-ingestion API — upload, status, page image, evidence.

``POST /v1/documents`` accepts a multipart upload (the scanned lab PDF / intake
form), authorizes it against the clinician's rounding list, ingests it through
the Week-2 pipeline (rasterize → OCR → structured extraction → OCR-reconcile →
append-only persist), and returns the ``202`` async-ingestion envelope
(``document_id`` + ``status`` + ``correlation_id``).

``GET /v1/documents/{id}`` reads the ingestion status plus the latest
extraction's schema-validated facts and their document-typed citations.
``GET /v1/documents/{id}/pages/{n}`` serves the rendered page image that the
bbox-overlay UI draws on.

When the OpenEMR write surface is unavailable (write-back off / unconfigured)
the pipeline runs with :class:`~copilot.documents.DerivedOnlyUploader`, so a
read-only deployment still ingests + extracts locally without pushing the source
document to OpenEMR.

Mounted automatically by ``copilot.api.app.register_routers`` (module-level
``router``); no edit to ``app.py`` is required.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any

from fastapi import (
    APIRouter,
    File,
    Form,
    HTTPException,
    Path,
    Query,
    Request,
    Response,
    UploadFile,
)

from copilot.api.deps import resolve_acting_context
from copilot.auth import is_authorized
from copilot.config import Settings, get_settings
from copilot.documents import DerivedOnlyUploader, DocumentIngestionService, DocumentUploader
from copilot.documents.vision import DocumentType
from copilot.domain.primitives import PatientId
from copilot.fhir.provider import build_write_client_for_session
from copilot.memory.db import session_scope
from copilot.memory.models import ExtractedFactRow
from copilot.memory.repository import MemoryRepository
from copilot.observability import current_correlation_id

router = APIRouter(prefix="/v1", tags=["documents"])

_UNAUTHORIZED_DETAIL = "Patient is not on your rounding list"
_INGESTION_DISABLED_DETAIL = "Document ingestion is disabled on this deployment"


def _uploader_factory(
    settings: Settings, session_id: str | None
) -> Callable[[], DocumentUploader] | None:
    """Choose the source-document uploader for this request.

    Prefers the real OpenEMR upload when the write surface is available: a
    smart-mode session rides the physician's delegated write token; disabled mode
    uses the default password-grant write client. When write-back is off, falls
    back to the derived-only uploader so ingestion still runs locally.
    """
    if not settings.writeback_enabled:
        return lambda: DerivedOnlyUploader()
    if session_id is not None:
        return lambda: build_write_client_for_session(settings, session_id)
    return None


def _fact_body(fact: ExtractedFactRow) -> dict[str, Any]:
    """One extracted fact with its reconciled page/bbox provenance."""
    return {
        "id": fact.id,
        "field_path": fact.field_path,
        "value": fact.value,
        "unit": fact.unit,
        "reference_range": fact.reference_range,
        "abnormal_flag": fact.abnormal_flag,
        "page_no": fact.page_no,
        "bbox": fact.bbox,
        "match_confidence": fact.match_confidence,
        "supported": fact.supported,
    }


def _citation_body(document_id: int, fact: ExtractedFactRow) -> dict[str, Any]:
    """A document-typed citation for one supported fact (pixel-level provenance)."""
    return {
        "source_type": "document",
        "source_id": str(document_id),
        "page_or_section": fact.page_no or 1,
        "field_or_chunk_id": str(fact.id),
        "quote_or_value": fact.value or "",
        "bbox": fact.bbox,
        "confidence": fact.match_confidence,
    }


@router.post(
    "/documents",
    status_code=202,
    summary="Upload a source document and ingest it (async envelope)",
)
async def upload_document(
    request: Request,
    file: Annotated[UploadFile, File(description="The scanned lab PDF / intake form.")],
    patient_id: Annotated[int, Form(gt=0)],
    # Optional on purpose, matching chat/observations/the document reads: in smart
    # mode identity comes from the session cookie and the UI sends no clinician_id,
    # so a REQUIRED field here 422s the upload before auth can run. In disabled mode
    # resolve_acting_context still demands an asserted id (400 when absent).
    clinician_id: Annotated[int | None, Form(gt=0)] = None,
    doc_type: Annotated[str, Form()] = "lab_pdf",
) -> dict[str, Any]:
    settings = get_settings()
    correlation_id = current_correlation_id()

    # Identity per the auth-mode contract (disabled → the form clinician_id;
    # smart → the session cookie, 401/403 on absence/mismatch). Resolved BEFORE
    # the ingestion kill switch below, matching the writes route (writes.py):
    # authz (401/403) runs before the disabled-feature 503 so feature
    # availability — whether ingestion is enabled on this deployment — never
    # leaks to an unauthenticated or unauthorized caller.
    acting = await resolve_acting_context(settings, request, clinician_id)
    cid = acting.clinician_id
    pid = PatientId(value=patient_id)

    # Authorization boundary (UC-6), identical to chat/writes: refuse a patient
    # the clinician has not established on their rounding list. A clinician with
    # no established round has an empty authorized set → refused.
    if not await is_authorized(cid, pid):
        raise HTTPException(status_code=403, detail=_UNAUTHORIZED_DETAIL)

    # The ingestion kill switch, genuinely enforced (it used to be declared in
    # config and read nowhere). Same contract as the writeback gate: a clear 503,
    # never a 500, and nothing is accepted or stored. Gated AFTER authz above so a
    # caller must already be authenticated and on-round to observe it.
    if not settings.document_ingestion_enabled:
        raise HTTPException(status_code=503, detail=_INGESTION_DISABLED_DETAIL)

    # Fail loud on an unknown doc_type instead of silently coercing it to lab_pdf
    # — a mis-typed upload would otherwise extract an intake form / medication
    # list with the wrong (lab) schema. Parse, don't silently default.
    if doc_type not in {kind.value for kind in DocumentType}:
        raise HTTPException(
            status_code=400,
            detail=f"unsupported doc_type '{doc_type}'; expected one of "
            f"{sorted(kind.value for kind in DocumentType)}",
        )

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")

    service = DocumentIngestionService(
        settings, write_client_factory=_uploader_factory(settings, acting.session_id)
    )
    result = await service.attach_and_extract(
        patient_id=pid,
        content=content,
        doc_type=doc_type,
        filename=file.filename,
        correlation_id=correlation_id,
    )
    return {
        "document_id": str(result.source_document_id),
        "status": result.status.value,
        "correlation_id": correlation_id,
    }


@router.get("/documents/{document_id}", summary="Ingestion status, facts, and citations")
async def get_document(
    document_id: Annotated[int, Path(gt=0)],
    request: Request,
    clinician_id: Annotated[int | None, Query(gt=0)] = None,
) -> dict[str, Any]:
    # Identity FIRST, before any read: this response carries extracted clinical
    # values (citations[].quote_or_value), so an unauthenticated caller must not
    # even learn whether a document id exists. Same auth-mode contract as the
    # upload/chat/observations routes (smart → session cookie, 401 if none).
    acting = await resolve_acting_context(get_settings(), request, clinician_id)
    cid = acting.clinician_id

    async with session_scope() as session:
        repo = MemoryRepository(session)
        doc = await repo.get_source_document(document_id)
        if doc is None:
            raise HTTPException(status_code=404, detail="Document not found")

        # Authorization boundary (UC-6): the document's patient must be on the
        # clinician's rounding list. Checked BEFORE reading the extraction and its
        # facts — an unauthorized caller must not cause PHI to be loaded at all, and
        # doing the work first made the refusal's latency scale with how many facts
        # the document has, which is itself a signal.
        #
        # Refuse with the SAME 404 (same detail) as the missing-document branch, NOT
        # a 403. A 403-vs-404 split turns the guessable autoincrement id space into an
        # existence oracle: walk 1..N and read "403 => this id exists, 404 => it does
        # not". The sibling conversation read route (GET /v1/conversations/{id}) and
        # the chat route deliberately unify both on 404 for exactly this reason;
        # mirror them so an off-round caller cannot even learn a document id is real.
        if not await is_authorized(cid, PatientId(value=doc.patient_id)):
            raise HTTPException(status_code=404, detail="Document not found")

        latest = await repo.get_latest_extraction(document_id)
        fact_rows = await repo.get_extracted_facts(latest.id) if latest is not None else []

    facts = [_fact_body(f) for f in fact_rows]
    citations = [_citation_body(document_id, f) for f in fact_rows if f.supported]
    return {
        "document_id": document_id,
        "patient_id": doc.patient_id,
        "status": doc.status,
        "doc_type": doc.doc_type,
        "page_count": doc.page_count,
        "openemr_document_id": doc.openemr_document_id,
        "correlation_id": doc.correlation_id,
        "extraction": {
            "extraction_id": latest.id if latest is not None else None,
            "model": latest.model if latest is not None else None,
            "schema_version": latest.schema_version if latest is not None else None,
            "confidence_overall": latest.confidence_overall if latest is not None else None,
            "facts": facts,
        },
        "citations": citations,
    }


@router.get(
    "/documents/{document_id}/pages/{page_no}",
    summary="The rendered page image (bbox-overlay backdrop)",
)
async def get_document_page(
    document_id: Annotated[int, Path(gt=0)],
    page_no: Annotated[int, Path(ge=1)],
    request: Request,
    clinician_id: Annotated[int | None, Query(gt=0)] = None,
) -> Response:
    # Identity FIRST: this returns the rendered page image of a scanned clinical
    # document — PHI. Unauthenticated callers must not reach the store at all.
    acting = await resolve_acting_context(get_settings(), request, clinician_id)
    cid = acting.clinician_id

    async with session_scope() as session:
        repo = MemoryRepository(session)
        doc = await repo.get_source_document(document_id)
        if doc is None:
            raise HTTPException(status_code=404, detail="Document not found")

        # Authorization boundary (UC-6): the document's patient must be on the
        # clinician's rounding list before any page bytes are returned. Checked
        # BEFORE loading the page image (get_document_pages reads a non-deferred
        # LargeBinary PNG blob — PHI) so an unauthorized caller never causes the
        # rasterized clinical page to be pulled from the store, and the refusal's
        # latency does not depend on whether the id exists (a missing id 404s
        # without a load; loading the image first turned that difference into a
        # timing oracle). Mirrors the get_document read above, which authorizes
        # before reading any facts.
        #
        # Refuse with the SAME 404 as the missing-document branch (not a 403) so a
        # 403-vs-404 split can never turn the guessable autoincrement id space into
        # an existence oracle — identical to the get_document / conversation read
        # contract above.
        if not await is_authorized(cid, PatientId(value=doc.patient_id)):
            raise HTTPException(status_code=404, detail="Document not found")

        pages = await repo.get_document_pages(document_id, page_no=page_no)

    if not pages or pages[0].image is None:
        raise HTTPException(status_code=404, detail="Page not found")
    return Response(content=pages[0].image, media_type="image/png")
