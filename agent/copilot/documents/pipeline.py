"""Document-ingestion pipeline: upload -> rasterize -> OCR -> extract -> reconcile -> persist.

``attach_and_extract`` (and the :class:`DocumentIngestionService` behind it) is
the single entry point that turns a source document's bytes into schema-validated,
bbox-anchored facts in the agent store. The flow, in order:

1. **Upload** the source bytes to OpenEMR via ``OpenEmrWriteClient.upload_document``
   (OpenEMR owns the document). Content-hash dedupe: identical bytes for the same
   patient upload exactly once and reuse the stored ``openemr_document_id``.
2. **Rasterize** every page (pypdfium2, at ``Settings.ocr_dpi``).
3. **OCR** each page image into word boxes (:class:`OcrEngine`).
4. **Extract** structured facts from the page images (:class:`VisionExtractor`),
   tool-forced JSON validated through the strict ``LabReport`` / ``IntakeForm``
   schemas.
5. **Reconcile** each value to the OCR tokens — attach a bbox + match confidence,
   or flag ``supported=False`` when the value is nowhere on the page.
6. **Persist** APPEND-ONLY through the F1 repository accessors (source_document /
   document_page / extraction / extracted_fact) with a correlation id + audit.

Status walks ``uploaded -> extracting -> extracted``; a mid-pipeline failure fails
closed — the attempt is recorded ``status='failed'`` with zero extraction rows and
zero orphan facts, and the error is surfaced (raised), never a silent success.
"""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from anyio import to_thread

from copilot.config import Settings, get_settings
from copilot.documents.ocr import OcrEngine, build_ocr
from copilot.documents.raster import RasterizedPage, rasterize_pdf
from copilot.documents.reconcile import Reconciliation, reconcile_value
from copilot.documents.vision import (
    SCHEMA_VERSION,
    DocumentType,
    ExtractionResult,
    VisionExtractor,
    build_vision,
    parse_doc_type,
)
from copilot.domain.documents import ExtractedFact, IntakeFact
from copilot.domain.primitives import PatientId
from copilot.fhir.provider import build_write_client
from copilot.memory.db import session_scope
from copilot.memory.repository import MemoryRepository
from copilot.observability import Observability, build_observability

_UPLOAD_CATEGORY = "copilot-ingested"


class DocumentUploader(Protocol):
    """The minimal write surface the pipeline needs to push a source document.

    The real :class:`~copilot.fhir.write_client.OpenEmrWriteClient` satisfies
    this structurally (it exposes exactly these members); the injection seam is
    typed against the protocol, not the concrete client, so a keyless/read-only
    deployment can substitute :class:`DerivedOnlyUploader` when the OpenEMR
    write surface is not configured.
    """

    async def __aenter__(self) -> DocumentUploader: ...

    async def __aexit__(self, *exc: object) -> None: ...

    async def upload_document(
        self,
        pid: PatientId,
        content: bytes,
        *,
        filename: str = ...,
        doc_type: str = ...,
        category: str | None = ...,
        mime_type: str = ...,
        idempotency_key: str | None = ...,
    ) -> str: ...


class DerivedOnlyUploader:
    """A no-op uploader: ingest + extract into the agent store WITHOUT pushing the
    source bytes to OpenEMR.

    Used when the OpenEMR write surface is unavailable (write-back disabled or its
    credentials unset) — a keyless / read-only deployment can still rasterize,
    OCR, extract, and persist the derived artifacts locally; it simply does not
    hand the source document to OpenEMR. Returns an empty document id so the
    ``source_document`` row records "no OpenEMR handle" rather than a fake one.
    """

    async def __aenter__(self) -> DerivedOnlyUploader:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def upload_document(
        self,
        pid: PatientId,
        content: bytes,
        *,
        filename: str = "document.pdf",
        doc_type: str = "lab_pdf",
        category: str | None = None,
        mime_type: str = "application/pdf",
        idempotency_key: str | None = None,
    ) -> str:
        return ""


class IngestionStatus(StrEnum):
    """Lifecycle of a source document through the pipeline."""

    uploaded = "uploaded"
    extracting = "extracting"
    extracted = "extracted"
    failed = "failed"


@dataclass(frozen=True)
class IngestionResult:
    """What one ``attach_and_extract`` call produced (on success)."""

    status: IngestionStatus
    source_document_id: int
    openemr_document_id: str | None
    extraction_id: int | None
    fact_count: int
    reused_upload: bool


class DocumentIngestionService:
    """Runs the ingestion pipeline for one deployment's configured collaborators.

    Collaborators are injectable for testing/DI; by default they are the
    settings-appropriate builders (keyless stubs when no key / no OCR binary).
    The OpenEMR upload uses the injected ``write_client_factory``; when the
    OpenEMR write surface is unavailable (write-back off / unconfigured), the F8
    HTTP route injects :class:`DerivedOnlyUploader` so ingestion still runs and
    persists the derived artifacts locally without pushing the source to OpenEMR.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        write_client_factory: Callable[[], DocumentUploader] | None = None,
        ocr: OcrEngine | None = None,
        vision: VisionExtractor | None = None,
        observability: Observability | None = None,
    ) -> None:
        self._settings = settings
        self._write_client_factory: Callable[[], DocumentUploader] = write_client_factory or (
            lambda: build_write_client(settings)
        )
        self._ocr = ocr or build_ocr(settings)
        self._vision = vision or build_vision(settings)
        # Defaults from settings like every other collaborator here, so the F8
        # upload route emits the span without an edit at its call site; keyless
        # (no Langfuse creds) resolves to NoopObservability — zero behaviour change.
        self._obs: Observability = observability or build_observability(settings)

    async def attach_and_extract(
        self,
        *,
        patient_id: PatientId,
        content: bytes,
        doc_type: str = "lab_pdf",
        filename: str | None = None,
        correlation_id: str = "",
    ) -> IngestionResult:
        """Ingest one document end-to-end. Fails closed on any mid-pipeline error.

        Wrapped in the ``doc.ingest`` span the OBSERVABILITY.md §7.1
        ingestion-latency SLO reads its p95 from, with the vision step as a
        nested ``extraction.run`` child. Span attributes are counts, ids and
        the document *type* only — never page text, OCR tokens, extracted
        clinical values, or the filename (which can itself carry a patient name).
        """
        pid = patient_id.value
        kind = parse_doc_type(doc_type)
        correlation = correlation_id or _new_correlation_id()
        content_hash = hashlib.sha256(content).hexdigest()

        async with self._obs.span(
            "doc.ingest", patient_id=pid, doc_type=kind.value, correlation_id=correlation
        ) as span:
            reuse = await _find_reusable_document(pid, content_hash)
            if reuse is not None:
                document_id, openemr_document_id = reuse
                reused = True
                pages, tokens_by_page = await _load_persisted_pages(document_id)
            else:
                document_id = await _create_pending_document(
                    pid, kind, filename, content_hash, correlation
                )
                reused = False
                openemr_document_id = await self._upload(document_id, pid, content, kind, filename)
                pages, tokens_by_page = await self._rasterize_and_ocr(document_id, content)
                await _mark_status(
                    document_id,
                    IngestionStatus.extracting,
                    openemr_document_id=openemr_document_id,
                    page_count=len(pages),
                )
                await _persist_pages(document_id, pages, tokens_by_page)

            facts = await self._extract(document_id, pages, kind, reused=reused)
            reconciled = _reconcile_facts(facts, tokens_by_page, self._settings)
            extraction_id, fact_count = await _persist_extraction(
                pid, document_id, self._vision.model_name, reconciled, correlation
            )
            span.set_attribute("source_document_id", document_id)
            span.set_attribute("page_count", len(pages))
            span.set_attribute("fact_count", fact_count)
            span.set_attribute("reused_upload", reused)
            span.set_attribute("status", IngestionStatus.extracted.value)
            span.set_output(
                {
                    "status": IngestionStatus.extracted.value,
                    "page_count": len(pages),
                    "fact_count": fact_count,
                    "reused_upload": reused,
                }
            )
            return IngestionResult(
                status=IngestionStatus.extracted,
                source_document_id=document_id,
                openemr_document_id=openemr_document_id,
                extraction_id=extraction_id,
                fact_count=fact_count,
                reused_upload=reused,
            )

    async def _upload(
        self,
        document_id: int,
        pid: int,
        content: bytes,
        kind: DocumentType,
        filename: str | None,
    ) -> str:
        """Upload the source bytes to OpenEMR; fail the ingestion closed on error."""
        try:
            async with self._write_client_factory() as client:
                return await client.upload_document(
                    PatientId(value=pid),
                    content,
                    filename=filename or f"document-{document_id}.pdf",
                    doc_type=kind.value,
                    category=_UPLOAD_CATEGORY,
                    mime_type="application/pdf",
                    idempotency_key=hashlib.sha256(content).hexdigest(),
                )
        except Exception:
            await _mark_status(document_id, IngestionStatus.failed)
            raise

    async def _rasterize_and_ocr(
        self, document_id: int, content: bytes
    ) -> tuple[list[RasterizedPage], dict[int, list[dict[str, object]]]]:
        """Render + OCR every page; fail closed if the bytes are not a readable PDF.

        The render + OCR work runs on a worker thread via ``to_thread.run_sync``,
        mirroring ``supervisor._review`` — because both ``rasterize_pdf`` and
        ``OcrEngine.recognize`` are synchronous, CPU-bound, and were being called
        straight from this coroutine. The upload route awaits ``attach_and_extract``
        inline in its handler, so every millisecond spent here was a millisecond
        the event loop could not serve anyone else: a 300-page PDF (a 36.5 KB
        upload, and an ordinary discharge summary) blocked the loop for 8.1
        seconds on raster ALONE, with OCR excluded — measured max stall for a
        concurrent request was 8102 ms. OCR is far slower per page than raster, so
        the real stall was minutes. One worker on one vCPU: no second core absorbs
        it, and every clinician loses chat, rounds, and document reads meanwhile.

        ``OcrEngine.recognize`` is a plain ``def`` on a Protocol with several
        implementors (``StubOcr``, ``TesseractOcr``, test fakes); making it async
        would be a breaking contract change for all of them and would force the
        pure-Python ``StubOcr`` to become a coroutine for no reason. Moving the
        existing sync body to a thread changes no semantics — the exception still
        propagates out of ``run_sync`` into the ``except`` below, so the
        fail-closed ``status='failed'`` transition is untouched.
        """
        try:
            return await to_thread.run_sync(self._rasterize_and_ocr_sync, content)
        except Exception:
            await _mark_status(document_id, IngestionStatus.failed)
            raise

    def _rasterize_and_ocr_sync(
        self, content: bytes
    ) -> tuple[list[RasterizedPage], dict[int, list[dict[str, object]]]]:
        """The synchronous render + OCR body. Runs on a worker thread.

        Caps come from ``Settings`` so an operator on a bigger box can raise them
        without a code change; the defaults are derived in ``raster.py``.
        """
        pages = rasterize_pdf(
            content,
            dpi=self._settings.ocr_dpi,
            max_page_pixels=self._settings.raster_max_page_pixels,
            max_pages=self._settings.raster_max_pages,
        )
        tokens_by_page: dict[int, list[dict[str, object]]] = {}
        for page in pages:
            tokens = self._ocr.recognize(
                page.image,
                page_no=page.page_no - 1,
                width=page.width,
                height=page.height,
            )
            tokens_by_page[page.page_no] = [token.to_dict() for token in tokens]
        return pages, tokens_by_page

    async def _extract(
        self,
        document_id: int,
        pages: Sequence[RasterizedPage],
        kind: DocumentType,
        *,
        reused: bool,
    ) -> list[ExtractedFact]:
        """Run structured extraction; fail closed on extraction/validation error.

        Opened inside ``doc.ingest``, so ``extraction.run`` is its child — the
        vision call is what dominates the ingestion SLO, and separating it lets
        a breach be attributed to the model call rather than to raster/OCR.

        ``reused`` gates the ``failed`` downgrade. A genuinely NEW ``document_id``
        that never extracted must record ``status='failed'`` on error (the
        fail-closed transition the module docstring promises). But on the dedupe
        REUSE path the ``document_id`` already holds a prior *successful*
        extraction — that success still stands. Marking it ``failed`` here would
        (a) corrupt a good document's status on a merely transient re-extract
        failure and (b) evict it from ``_find_reusable_document`` (which excludes
        ``failed``), so the next identical-bytes ingest would mint a DUPLICATE
        ``source_document`` row. On reuse the error therefore propagates WITHOUT
        touching status, leaving the prior ``extracted`` state intact.
        """
        async with self._obs.span(
            "extraction.run", doc_type=kind.value, page_count=len(pages)
        ) as span:
            span.set_attribute("model", self._vision.model_name)
            try:
                report: ExtractionResult = await self._vision.extract(pages, kind)
            except Exception:
                span.set_attribute("failed", True)
                if not reused:
                    await _mark_status(document_id, IngestionStatus.failed)
                raise
            facts = list(report.facts)
            span.set_attribute("fact_count", len(facts))
            span.set_output({"fact_count": len(facts)})
            return facts


async def attach_and_extract(
    *,
    patient_id: PatientId,
    content: bytes,
    doc_type: str = "lab_pdf",
    filename: str | None = None,
    correlation_id: str = "",
    settings: Settings | None = None,
) -> IngestionResult:
    """Ingest one document with the deployment's default collaborators.

    Thin convenience wrapper over :class:`DocumentIngestionService` for callers
    that do not need to inject collaborators (tests, CLI).

    NOT used by the upload route: ``api/routes/documents.py`` constructs the
    service directly so it can inject the uploader its config implies
    (``DerivedOnlyUploader`` when write-back is off — see ``routes/documents.py``
    ``_uploader_factory``). This wrapper takes the DEFAULT factory, i.e. a real
    OpenEMR write client, so calling it with write-back disabled raises
    ``WritebackDisabledError`` — which is correct, but is NOT what a browser
    upload does. Reach for the service directly if you care which uploader runs.
    """
    service = DocumentIngestionService(settings or get_settings())
    return await service.attach_and_extract(
        patient_id=patient_id,
        content=content,
        doc_type=doc_type,
        filename=filename,
        correlation_id=correlation_id,
    )


# --- reconciliation ---------------------------------------------------------


def _reconcile_facts(
    facts: Sequence[ExtractedFact],
    tokens_by_page: Mapping[int, list[dict[str, object]]],
    settings: Settings,
) -> list[tuple[ExtractedFact, Reconciliation]]:
    """Reconcile each extracted value to *its own* page's OCR tokens — never another's.

    A fact is only ever searched against the page it names. This used to fall back
    to page 1's tokens whenever the named page's were falsy, which inverted the
    no-invention gate on precisely the pages it exists to protect: an empty list is
    falsy, so a page the vision model can read but OCR cannot — handwriting, a
    photographed or angled page, a low-contrast fax, a rotated scan, all routine on
    clinical intake — was silently reconciled against page 1 instead. The fact then
    won ``supported=True`` at a high confidence on a genuine exact string match
    (page 1's "DOB: 10 mg" header blessing a handwritten dose on page 3) while
    keeping its own page number, and the citation stored page 1's bbox under page 3
    — a highlight drawn where the value is not. Neither the confidence threshold nor
    ``vision._check_page_numbers`` can catch that: the match is real and the page
    number is real; only the pairing is a lie. An unsearchable page has exactly one
    honest answer, and reconciliation already gives it for empty tokens —
    ``supported=False``, no bbox, surfaced as unverified.

    A fact with no ``page_no`` is searched only when the document has exactly ONE
    page, where "page 1" is not a fallback but the only page the fact can have come
    from — the sole page is named by elimination. On a multi-page document an
    unnumbered fact is refused rather than guessed at page 1, which is the milder
    half of the same bug (a coincidental header match blessed as provenance).
    """
    threshold = settings.doc_extraction_confidence_threshold
    # By elimination, not by default: only meaningful when there is one page.
    sole_page = next(iter(tokens_by_page)) if len(tokens_by_page) == 1 else None
    out: list[tuple[ExtractedFact, Reconciliation]] = []
    for fact in facts:
        page_no = fact.page_no if fact.page_no is not None else sole_page
        # .get(page_no, []) — never another page's tokens. A page that is present
        # but OCR'd to nothing reconciles to nothing, which is the honest answer.
        tokens = tokens_by_page.get(page_no, []) if page_no is not None else []
        recon = reconcile_value(fact.value, tokens, page_no=page_no or 1, threshold=threshold)
        out.append((fact, recon))
    return out


# --- persistence (append-only, via the F1 repository accessors) -------------


async def _find_reusable_document(pid: int, content_hash: str) -> tuple[int, str] | None:
    """A prior, successfully-uploaded document for these exact bytes, if any.

    Dedupe key is ``(patient_id, content_hash)`` with a stored
    ``openemr_document_id`` and a non-failed status — so identical bytes upload to
    OpenEMR exactly once. Routed through
    ``MemoryRepository.get_source_document_by_hash`` (read-only).
    """
    async with session_scope() as session:
        row = await MemoryRepository(session).get_source_document_by_hash(
            pid, content_hash, exclude_status=IngestionStatus.failed.value
        )
    if row is None or row.openemr_document_id is None:
        return None
    return row.id, row.openemr_document_id


async def _create_pending_document(
    pid: int,
    kind: DocumentType,
    filename: str | None,
    content_hash: str,
    correlation: str,
) -> int:
    """Register the ingestion attempt (status='uploaded') and audit its start."""
    async with session_scope() as session:
        repo = MemoryRepository(session)
        row = await repo.create_source_document(
            patient_id=pid,
            doc_type=kind.value,
            correlation_id=correlation,
            filename=filename,
            content_hash=content_hash,
            status=IngestionStatus.uploaded.value,
        )
        await repo.record_audit(
            correlation_id=correlation,
            action="document.ingest",
            patient_id=PatientId(value=pid),
        )
        return row.id


async def _mark_status(
    document_id: int,
    status: IngestionStatus,
    *,
    openemr_document_id: str | None = None,
    page_count: int | None = None,
) -> None:
    """Commit a status transition (and optional upload id / page count) in its own txn.

    Its own committed transaction so the failed-status attempt survives even when
    the pipeline then re-raises the triggering error.
    """
    async with session_scope() as session:
        row = await MemoryRepository(session).get_source_document(document_id)
        if row is None:
            return
        row.status = status.value
        if openemr_document_id is not None:
            row.openemr_document_id = openemr_document_id
        if page_count is not None:
            row.page_count = page_count
        await session.flush()


async def _persist_pages(
    document_id: int,
    pages: Sequence[RasterizedPage],
    tokens_by_page: Mapping[int, list[dict[str, object]]],
) -> None:
    """Persist each rendered page + its OCR word boxes."""
    async with session_scope() as session:
        repo = MemoryRepository(session)
        for page in pages:
            await repo.create_document_page(
                source_document_id=document_id,
                page_no=page.page_no,
                image=page.image,
                width=page.width,
                height=page.height,
                ocr_tokens=list(tokens_by_page.get(page.page_no, [])),
            )


async def _load_persisted_pages(
    document_id: int,
) -> tuple[list[RasterizedPage], dict[int, list[dict[str, object]]]]:
    """Re-hydrate a prior document's page renders + OCR tokens (dedupe reuse path)."""
    async with session_scope() as session:
        rows = await MemoryRepository(session).get_document_pages(document_id)
        pages = [
            RasterizedPage(
                page_no=row.page_no,
                width=row.width or 0,
                height=row.height or 0,
                image=row.image or b"",
            )
            for row in rows
        ]
        tokens_by_page = {row.page_no: list(row.ocr_tokens or []) for row in rows}
    return pages, tokens_by_page


async def _persist_extraction(
    pid: int,
    document_id: int,
    model: str,
    reconciled: Sequence[tuple[ExtractedFact, Reconciliation]],
    correlation: str,
) -> tuple[int, int]:
    """Append one extraction run + its facts; audit + walk status to 'extracted'.

    APPEND-ONLY: a fresh ``extraction`` row (and its facts) every call; prior runs
    are never mutated. Committed as one transaction, so a re-ingest adds exactly
    one extraction with its facts.
    """
    supported = [recon.match_confidence for _, recon in reconciled if recon.supported]
    confidence_overall = sum(supported) / len(supported) if supported else None
    async with session_scope() as session:
        repo = MemoryRepository(session)
        extraction = await repo.create_extraction(
            source_document_id=document_id,
            correlation_id=correlation,
            schema_version=SCHEMA_VERSION,
            model=model,
            confidence_overall=confidence_overall,
            status="ok",
        )
        for fact, recon in reconciled:
            await repo.create_extracted_fact(
                extraction_id=extraction.id,
                field_path=fact.field_path,
                value=fact.value,
                unit=fact.unit,
                reference_range=fact.reference_range,
                abnormal_flag=fact.abnormal,
                collection_date=fact.collection_date,
                page_no=recon.page_no if recon.supported else fact.page_no,
                bbox=recon.bbox,
                match_confidence=recon.match_confidence if recon.supported else None,
                supported=recon.supported,
                category=fact.category.value if isinstance(fact, IntakeFact) else None,
            )
        await repo.record_audit(
            correlation_id=correlation,
            action="extraction.run",
            patient_id=PatientId(value=pid),
        )
        row = await repo.get_source_document(document_id)
        if row is not None:
            row.status = IngestionStatus.extracted.value
        await session.flush()
        return extraction.id, len(reconciled)


def _new_correlation_id() -> str:
    """A fresh, valid CorrelationId for a direct (non-request) invocation."""
    return f"doc-ingest-{secrets.token_hex(8)}"
