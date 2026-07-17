"""Async repository over the agent-owned Postgres.

Everything that touches the DB goes through here — never raw SQL from
application code.  Contracts (Pydantic) go in, Contracts come out; SQL
never leaks to callers.  See ARCHITECTURE §"Components → memory-store".
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from copilot.domain.contracts import (
    Claim,
    ClaimSeverity,
    MemoryFileSummary,
    TrendDirection,
    ValueDirection,
)
from copilot.domain.primitives import (
    CitationSourceType,
    ClinicianId,
    DocumentCitation,
    FhirReference,
    GuidelineCitation,
    PatientId,
    ResourceType,
    utcnow,
)
from copilot.memory.models import (
    AuditLogRow,
    ClinicianRow,
    ConversationRow,
    DocumentPageRow,
    ExtractedFactRow,
    ExtractionRow,
    GuidelineChunkRow,
    GuidelineDocumentRow,
    LastSeenRow,
    LoginTxnRow,
    MemoryFileRow,
    MessageRow,
    PhysicianSessionRow,
    RoundingCursorRow,
    SourceDocumentRow,
    SyncStateRow,
)
from copilot.memory.records import ConversationMessage, RoundingCursor


class MemoryRepository:
    """One instance per Session — do NOT share across event loop tasks."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # --- sync_state -------------------------------------------------------

    async def get_sync_state(self, patient_id: PatientId) -> SyncStateRow | None:
        result = await self._session.execute(
            select(SyncStateRow).where(SyncStateRow.patient_id == patient_id.value)
        )
        return result.scalar_one_or_none()

    async def upsert_sync_state(
        self,
        patient_id: PatientId,
        *,
        polled_at: datetime,
        success_at: datetime | None,
        watermark: datetime | None,
        content_hash: str,
        consecutive_failures: int,
    ) -> SyncStateRow:
        row = await self.get_sync_state(patient_id)
        if row is None:
            row = SyncStateRow(patient_id=patient_id.value)
            self._session.add(row)
        row.last_polled_at = polled_at
        if success_at is not None:
            row.last_success_at = success_at
        if watermark is not None:
            row.watermark = watermark
        row.content_hash = content_hash
        row.consecutive_failures = consecutive_failures
        await self._session.flush()
        return row

    # --- memory_file ------------------------------------------------------

    async def get_memory_file(self, patient_id: PatientId) -> MemoryFileSummary | None:
        result = await self._session.execute(
            select(MemoryFileRow).where(MemoryFileRow.patient_id == patient_id.value)
        )
        row = result.scalar_one_or_none()
        if row is None:
            return None
        return _row_to_summary(row)

    async def save_memory_file(self, summary: MemoryFileSummary) -> None:
        existing = await self._session.execute(
            select(MemoryFileRow).where(MemoryFileRow.patient_id == summary.patient_id.value)
        )
        row = existing.scalar_one_or_none()
        payload = _summary_to_json(summary)
        if row is None:
            row = MemoryFileRow(patient_id=summary.patient_id.value)
            self._session.add(row)
        row.summary = payload
        row.acuity_score = summary.acuity_score
        row.rank_reason = summary.rank_reason
        row.synthesized_at = summary.synthesized_at.replace(tzinfo=None)
        row.source_watermark = summary.source_watermark.replace(tzinfo=None)
        row.content_hash = summary.content_hash
        row.stale = False
        await self._session.flush()

    # --- audit ------------------------------------------------------------

    async def record_audit(
        self,
        *,
        correlation_id: str,
        action: str,
        patient_id: PatientId | None = None,
        clinician_id: int | None = None,
        resources_returned: list[str] | None = None,
        entry_mode: str | None = None,
    ) -> None:
        """Append one access/write trail row.

        ``entry_mode`` is the write-back physician-attribution field
        (``human_direct`` in Phase 1); it defaults to ``None`` so every existing
        read-audit caller is unaffected.
        """
        self._session.add(
            AuditLogRow(
                correlation_id=correlation_id,
                action=action,
                patient_id=patient_id.value if patient_id else None,
                clinician_id=clinician_id,
                resources_returned=resources_returned or [],
                entry_mode=entry_mode,
                at=utcnow().replace(tzinfo=None),
            )
        )
        await self._session.flush()

    # --- conversations / messages ----------------------------------------

    async def create_conversation(
        self, clinician_id: ClinicianId, patient_id: PatientId, correlation_id: str
    ) -> int:
        """Open a new patient-scoped chat session; return its new id."""
        row = ConversationRow(
            clinician_id=clinician_id.value,
            patient_id=patient_id.value,
            correlation_id=correlation_id,
        )
        self._session.add(row)
        await self._session.flush()
        return row.id

    async def append_message(self, conversation_id: int, role: str, content: str) -> None:
        """Append one turn to a conversation."""
        self._session.add(MessageRow(conversation_id=conversation_id, role=role, content=content))
        await self._session.flush()

    async def get_conversation_messages(self, conversation_id: int) -> list[ConversationMessage]:
        """Read back a conversation's turns, oldest first (created_at, then id)."""
        result = await self._session.execute(
            select(MessageRow)
            .where(MessageRow.conversation_id == conversation_id)
            .order_by(MessageRow.created_at, MessageRow.id)
        )
        return [
            ConversationMessage(role=row.role, content=row.content, created_at=row.created_at)
            for row in result.scalars().all()
        ]

    # --- rounding_cursor --------------------------------------------------

    async def get_rounding_cursor(self, clinician_id: ClinicianId) -> RoundingCursor | None:
        result = await self._session.execute(
            select(RoundingCursorRow).where(RoundingCursorRow.clinician_id == clinician_id.value)
        )
        row = result.scalar_one_or_none()
        if row is None:
            return None
        return RoundingCursor(
            clinician_id=clinician_id,
            ordered_patient_ids=list(row.ordered_patient_ids),
            current_index=row.current_index,
            completed_ids=list(row.completed_ids),
        )

    async def upsert_rounding_cursor(
        self,
        clinician_id: ClinicianId,
        ordered_patient_ids: list[int],
        current_index: int,
        completed_ids: list[int],
    ) -> None:
        result = await self._session.execute(
            select(RoundingCursorRow).where(RoundingCursorRow.clinician_id == clinician_id.value)
        )
        row = result.scalar_one_or_none()
        if row is None:
            row = RoundingCursorRow(clinician_id=clinician_id.value)
            self._session.add(row)
        row.ordered_patient_ids = ordered_patient_ids
        row.current_index = current_index
        row.completed_ids = completed_ids
        row.updated_at = utcnow().replace(tzinfo=None)
        await self._session.flush()

    # --- last_seen --------------------------------------------------------

    async def set_last_seen(
        self,
        clinician_id: ClinicianId,
        patient_id: PatientId,
        seen_at: datetime | None = None,
    ) -> None:
        """Mark (clinician, patient) as seen; upsert on the unique pair."""
        when = (seen_at if seen_at is not None else utcnow()).replace(tzinfo=None)
        result = await self._session.execute(
            select(LastSeenRow).where(
                LastSeenRow.clinician_id == clinician_id.value,
                LastSeenRow.patient_id == patient_id.value,
            )
        )
        row = result.scalar_one_or_none()
        if row is None:
            row = LastSeenRow(clinician_id=clinician_id.value, patient_id=patient_id.value)
            self._session.add(row)
        row.seen_at = when
        await self._session.flush()

    async def get_last_seen(
        self, clinician_id: ClinicianId, patient_id: PatientId
    ) -> datetime | None:
        result = await self._session.execute(
            select(LastSeenRow.seen_at).where(
                LastSeenRow.clinician_id == clinician_id.value,
                LastSeenRow.patient_id == patient_id.value,
            )
        )
        return result.scalar_one_or_none()

    # --- clinician mapping (SMART login) ---------------------------------
    #
    # The int-keyed tables (rounding_cursor/audit_log/last_seen/conversation)
    # are unchanged; this table just mints the stable integer surrogate for an
    # OpenEMR fhirUser. Unused while auth_mode="disabled".

    async def get_clinician_by_fhir_user(self, fhir_user: str) -> ClinicianRow | None:
        result = await self._session.execute(
            select(ClinicianRow).where(ClinicianRow.fhir_user == fhir_user)
        )
        return result.scalar_one_or_none()

    async def create_clinician(
        self,
        *,
        fhir_user: str,
        openemr_username: str | None,
        display_name: str | None,
        npi: str | None,
    ) -> ClinicianRow:
        """Auto-provision a clinician on first login; returns the new row (with id)."""
        row = ClinicianRow(
            fhir_user=fhir_user,
            openemr_username=openemr_username,
            display_name=display_name,
            npi=npi,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def set_clinician_last_login(self, clinician_id: int, at: datetime) -> None:
        result = await self._session.execute(
            select(ClinicianRow).where(ClinicianRow.id == clinician_id)
        )
        row = result.scalar_one_or_none()
        if row is None:
            return
        row.last_login_at = at
        await self._session.flush()

    # --- physician_session (opaque server session) -----------------------

    async def create_physician_session(
        self,
        *,
        session_id: str,
        clinician_id: int,
        access_token_enc: bytes,
        refresh_token_enc: bytes | None,
        access_expires_at: datetime,
        scope: str | None,
        fhir_user: str,
        created_at: datetime,
        absolute_expires_at: datetime,
    ) -> None:
        self._session.add(
            PhysicianSessionRow(
                session_id=session_id,
                clinician_id=clinician_id,
                access_token_enc=access_token_enc,
                refresh_token_enc=refresh_token_enc,
                access_expires_at=access_expires_at,
                scope=scope,
                fhir_user=fhir_user,
                created_at=created_at,
                last_used_at=created_at,
                absolute_expires_at=absolute_expires_at,
                revoked=False,
            )
        )
        await self._session.flush()

    async def get_physician_session(self, session_id: str) -> PhysicianSessionRow | None:
        result = await self._session.execute(
            select(PhysicianSessionRow).where(PhysicianSessionRow.session_id == session_id)
        )
        return result.scalar_one_or_none()

    async def touch_physician_session(self, session_id: str, last_used_at: datetime) -> None:
        """Sliding-window refresh of ``last_used_at`` on activity."""
        row = await self.get_physician_session(session_id)
        if row is None:
            return
        row.last_used_at = last_used_at
        await self._session.flush()

    async def rotate_physician_session_token(
        self,
        session_id: str,
        *,
        access_token_enc: bytes,
        refresh_token_enc: bytes | None,
        access_expires_at: datetime,
        scope: str | None,
    ) -> None:
        """Persist a rotated access/refresh token back to the session row.

        OpenEMR rotates refresh tokens, so the freshly-issued material replaces
        the prior ciphertext in place; the opaque cookie (and PK) are unchanged.
        """
        row = await self.get_physician_session(session_id)
        if row is None:
            return
        row.access_token_enc = access_token_enc
        if refresh_token_enc is not None:
            row.refresh_token_enc = refresh_token_enc
        row.access_expires_at = access_expires_at
        if scope is not None:
            row.scope = scope
        await self._session.flush()

    async def revoke_physician_session(self, session_id: str) -> None:
        row = await self.get_physician_session(session_id)
        if row is None:
            return
        row.revoked = True
        await self._session.flush()

    # --- login_txn (short-lived OAuth state + PKCE verifier) -------------

    async def create_login_txn(
        self,
        *,
        state: str,
        code_verifier: str,
        nonce: str,
        redirect_target: str | None,
        created_at: datetime,
        expires_at: datetime,
    ) -> None:
        self._session.add(
            LoginTxnRow(
                state=state,
                code_verifier=code_verifier,
                nonce=nonce,
                redirect_target=redirect_target,
                created_at=created_at,
                expires_at=expires_at,
            )
        )
        await self._session.flush()

    async def consume_login_txn(self, state: str) -> LoginTxnRow | None:
        """Fetch-and-delete a login transaction (single use).

        Returns the row (detached, attributes loaded) so the caller can read the
        ``code_verifier``/``redirect_target``; deletes it so a ``state`` can never
        be replayed. Returns ``None`` when the state is unknown.
        """
        row = await self.get_login_txn(state)
        if row is None:
            return None
        await self._session.delete(row)
        await self._session.flush()
        return row

    async def get_login_txn(self, state: str) -> LoginTxnRow | None:
        result = await self._session.execute(select(LoginTxnRow).where(LoginTxnRow.state == state))
        return result.scalar_one_or_none()

    # --- Week-2 document ingestion (source_document / document_page / extraction /
    #     extracted_fact) + guideline corpus (guideline_document / guideline_chunk) --
    #
    # Phase-0 CRUD gateway: create/get accessors over the six W2 tables. Contracts
    # in, rows out, no SQL leaks. Grounding (F5) reads back the stored, immutable
    # rows through these; the append-only extraction discipline lives in the models.

    async def create_source_document(
        self,
        *,
        patient_id: int,
        doc_type: str,
        correlation_id: str,
        openemr_document_id: str | None = None,
        category_path: str | None = None,
        filename: str | None = None,
        content_hash: str = "",
        page_count: int = 0,
        status: str = "uploaded",
        uploaded_by: int | None = None,
    ) -> SourceDocumentRow:
        """Register an agent-side handle for a document uploaded to OpenEMR."""
        row = SourceDocumentRow(
            patient_id=patient_id,
            doc_type=doc_type,
            correlation_id=correlation_id,
            openemr_document_id=openemr_document_id,
            category_path=category_path,
            filename=filename,
            content_hash=content_hash,
            page_count=page_count,
            status=status,
            uploaded_by=uploaded_by,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def get_source_document(self, document_id: int) -> SourceDocumentRow | None:
        result = await self._session.execute(
            select(SourceDocumentRow).where(SourceDocumentRow.id == document_id)
        )
        return result.scalar_one_or_none()

    async def get_source_document_by_hash(
        self,
        patient_id: int,
        content_hash: str,
        *,
        exclude_status: str,
    ) -> SourceDocumentRow | None:
        """A prior, successfully-uploaded source document for these exact bytes, if any.

        The dedupe key is ``(patient_id, content_hash)`` narrowed to rows that hold a
        stored ``openemr_document_id`` and whose status is not ``exclude_status`` (the
        pipeline passes ``failed``), so identical bytes upload to OpenEMR exactly once.
        Lowest id first. Backs the ingestion pipeline's content-hash dedupe lookup.
        """
        result = await self._session.execute(
            select(SourceDocumentRow)
            .where(
                SourceDocumentRow.patient_id == patient_id,
                SourceDocumentRow.content_hash == content_hash,
                SourceDocumentRow.openemr_document_id.is_not(None),
                SourceDocumentRow.status != exclude_status,
            )
            .order_by(SourceDocumentRow.id)
        )
        return result.scalars().first()

    async def create_document_page(
        self,
        *,
        source_document_id: int,
        page_no: int,
        image: bytes | None = None,
        width: int | None = None,
        height: int | None = None,
        ocr_tokens: list[dict[str, Any]] | None = None,
    ) -> DocumentPageRow:
        """Persist one rasterized page render + its OCR word boxes."""
        row = DocumentPageRow(
            source_document_id=source_document_id,
            page_no=page_no,
            image=image,
            width=width,
            height=height,
            ocr_tokens=ocr_tokens if ocr_tokens is not None else [],
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def get_document_pages(
        self, source_document_id: int, page_no: int | None = None
    ) -> list[DocumentPageRow]:
        """All pages of a document (ascending), optionally narrowed to one page."""
        stmt = select(DocumentPageRow).where(
            DocumentPageRow.source_document_id == source_document_id
        )
        if page_no is not None:
            stmt = stmt.where(DocumentPageRow.page_no == page_no)
        result = await self._session.execute(stmt.order_by(DocumentPageRow.page_no))
        return list(result.scalars().all())

    async def create_extraction(
        self,
        *,
        source_document_id: int,
        correlation_id: str,
        schema_version: str = "",
        model: str | None = None,
        confidence_overall: float | None = None,
        status: str = "ok",
    ) -> ExtractionRow:
        """Append one extraction run over a source document (never mutates prior runs)."""
        row = ExtractionRow(
            source_document_id=source_document_id,
            correlation_id=correlation_id,
            schema_version=schema_version,
            model=model,
            confidence_overall=confidence_overall,
            status=status,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def get_extraction(self, extraction_id: int) -> ExtractionRow | None:
        result = await self._session.execute(
            select(ExtractionRow).where(ExtractionRow.id == extraction_id)
        )
        return result.scalar_one_or_none()

    async def get_latest_extraction(self, source_document_id: int) -> ExtractionRow | None:
        """The most recent extraction run for a document (extractions are append-only)."""
        result = await self._session.execute(
            select(ExtractionRow)
            .where(ExtractionRow.source_document_id == source_document_id)
            .order_by(ExtractionRow.id.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def create_extracted_fact(
        self,
        *,
        extraction_id: int,
        field_path: str,
        value: str | None = None,
        unit: str | None = None,
        reference_range: str | None = None,
        abnormal_flag: str | None = None,
        collection_date: datetime | None = None,
        page_no: int | None = None,
        bbox: list[float] | None = None,
        match_confidence: float | None = None,
        supported: bool = False,
        category: str | None = None,
    ) -> ExtractedFactRow:
        """Persist one schema-validated fact with its reconciled page/bbox provenance.

        ``category`` is the OpenEMR record type for an intake fact (an
        ``IntakeCategory`` value) and ``None`` for lab facts.
        """
        row = ExtractedFactRow(
            extraction_id=extraction_id,
            field_path=field_path,
            value=value,
            unit=unit,
            reference_range=reference_range,
            abnormal_flag=abnormal_flag,
            collection_date=collection_date,
            page_no=page_no,
            bbox=bbox,
            match_confidence=match_confidence,
            supported=supported,
            category=category,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def get_extracted_facts(self, extraction_id: int) -> list[ExtractedFactRow]:
        """Every fact produced by one extraction run (ascending id)."""
        result = await self._session.execute(
            select(ExtractedFactRow)
            .where(ExtractedFactRow.extraction_id == extraction_id)
            .order_by(ExtractedFactRow.id)
        )
        return list(result.scalars().all())

    async def get_supported_extracted_facts(
        self, extraction_id: int
    ) -> list[ExtractedFactRow]:
        """Only the *supported* facts of one extraction run (``supported`` is True).

        Unlike :meth:`get_extracted_facts`, this filters to facts that passed the
        no-invention gate; the intake-extractor counts them to report supported-fact
        totals, so no ordering is imposed.
        """
        result = await self._session.execute(
            select(ExtractedFactRow).where(
                ExtractedFactRow.extraction_id == extraction_id,
                ExtractedFactRow.supported.is_(True),
            )
        )
        return list(result.scalars().all())

    async def get_extracted_fact_by_id(
        self, fact_id: int, source_document_id: int
    ) -> ExtractedFactRow | None:
        """One extracted fact by id, bound to a source document through its extraction.

        The join to ``extraction`` scopes the fact to its cited source document, so a
        fact id that belongs to a different document does not match. Backs serve-time
        document grounding (verification re-fetch by ``(source_document, fact)`` id).
        """
        result = await self._session.execute(
            select(ExtractedFactRow)
            .join(ExtractionRow, ExtractedFactRow.extraction_id == ExtractionRow.id)
            .where(
                ExtractedFactRow.id == fact_id,
                ExtractionRow.source_document_id == source_document_id,
            )
        )
        return result.scalar_one_or_none()

    async def create_guideline_document(
        self,
        *,
        title: str,
        source: str | None = None,
        license: str | None = None,
    ) -> GuidelineDocumentRow:
        """Register one source guideline document in the local corpus."""
        row = GuidelineDocumentRow(title=title, source=source, license=license)
        self._session.add(row)
        await self._session.flush()
        return row

    async def get_guideline_document(
        self, guideline_document_id: int
    ) -> GuidelineDocumentRow | None:
        result = await self._session.execute(
            select(GuidelineDocumentRow).where(GuidelineDocumentRow.id == guideline_document_id)
        )
        return result.scalar_one_or_none()

    async def get_guideline_document_by_source(
        self, source: str
    ) -> GuidelineDocumentRow | None:
        """One guideline document by its ``source`` identifier, if present.

        Backs the corpus-ingest idempotency probe: a returned row means this source is
        already ingested (the ingester inserts each source at most once, so at most one
        row matches).
        """
        result = await self._session.execute(
            select(GuidelineDocumentRow).where(GuidelineDocumentRow.source == source)
        )
        return result.scalar_one_or_none()

    async def create_guideline_chunk(
        self,
        *,
        guideline_document_id: int,
        content: str,
        section: str | None = None,
        chunk_index: int = 0,
        embedding: list[float] | None = None,
    ) -> GuidelineChunkRow:
        """Persist one retrievable chunk (text + dense embedding + section)."""
        row = GuidelineChunkRow(
            guideline_document_id=guideline_document_id,
            content=content,
            section=section,
            chunk_index=chunk_index,
            embedding=embedding,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def get_guideline_chunks(
        self, guideline_document_id: int
    ) -> list[GuidelineChunkRow]:
        """Every chunk of a guideline document, in chunk order."""
        result = await self._session.execute(
            select(GuidelineChunkRow)
            .where(GuidelineChunkRow.guideline_document_id == guideline_document_id)
            .order_by(GuidelineChunkRow.chunk_index)
        )
        return list(result.scalars().all())

    async def list_guideline_chunks(self) -> list[GuidelineChunkRow]:
        """Every guideline chunk across the whole corpus, ordered by id.

        Unlike :meth:`get_guideline_chunks`, this is not scoped to one document: the
        hybrid retriever loads the full chunk set to rank it in memory.
        """
        result = await self._session.execute(
            select(GuidelineChunkRow).order_by(GuidelineChunkRow.id)
        )
        return list(result.scalars().all())

    async def get_guideline_chunk_by_id(
        self, chunk_id: int, guideline_document_id: int
    ) -> GuidelineChunkRow | None:
        """One guideline chunk by id, scoped to its guideline document.

        The document scope means a chunk id that belongs to a different document does
        not match. Backs serve-time guideline grounding (verification re-fetch by
        ``(document, chunk)`` id).
        """
        result = await self._session.execute(
            select(GuidelineChunkRow).where(
                GuidelineChunkRow.id == chunk_id,
                GuidelineChunkRow.guideline_document_id == guideline_document_id,
            )
        )
        return result.scalar_one_or_none()


# --- (de)serialization ------------------------------------------------------


def _citation_to_json(
    ref: FhirReference | DocumentCitation | GuidelineCitation,
) -> dict[str, Any]:
    """Serialize a claim citation, tagged by its ``source_type`` discriminator.

    Hand-written (rather than ``model_dump``) so the on-disk shape is pinned and
    the deserializer below is its exact inverse — a load→save cycle is byte-equal.
    """
    if isinstance(ref, DocumentCitation):
        return {
            "source_type": CitationSourceType.document.value,
            "source_id": ref.source_id,
            "page_or_section": ref.page_or_section,
            "field_or_chunk_id": ref.field_or_chunk_id,
            "quote_or_value": ref.quote_or_value,
            "bbox": list(ref.bbox) if ref.bbox is not None else None,
            "confidence": ref.confidence,
        }
    if isinstance(ref, GuidelineCitation):
        return {
            "source_type": CitationSourceType.guideline.value,
            "source_id": ref.source_id,
            "page_or_section": ref.page_or_section,
            "field_or_chunk_id": ref.field_or_chunk_id,
            "quote_or_value": ref.quote_or_value,
        }
    return {
        "source_type": CitationSourceType.fhir.value,
        "resource_type": ref.resource_type.value,
        "resource_id": ref.resource_id,
        "field": ref.field,
        "value": ref.value,
        "last_updated": ref.last_updated.isoformat() if ref.last_updated else None,
        "timestamp": ref.timestamp.isoformat() if ref.timestamp else None,
    }


def _citation_from_json(
    ref: dict[str, Any],
) -> FhirReference | DocumentCitation | GuidelineCitation:
    """Rehydrate a citation, dispatching on ``source_type``.

    Back-compat: a Week-1 row carries no ``source_type``, so ``.get`` defaults it
    to ``fhir`` and the legacy shape rehydrates as a :class:`FhirReference`
    unchanged.
    """
    source_type = ref.get("source_type", CitationSourceType.fhir.value)
    if source_type == CitationSourceType.document.value:
        return DocumentCitation(
            source_id=ref["source_id"],
            page_or_section=ref["page_or_section"],
            field_or_chunk_id=ref["field_or_chunk_id"],
            quote_or_value=ref["quote_or_value"],
            bbox=ref.get("bbox"),
            confidence=ref.get("confidence"),
        )
    if source_type == CitationSourceType.guideline.value:
        return GuidelineCitation(
            source_id=ref["source_id"],
            page_or_section=ref["page_or_section"],
            field_or_chunk_id=ref["field_or_chunk_id"],
            quote_or_value=ref["quote_or_value"],
        )
    last_upd = ref.get("last_updated")
    # Older rows predate `timestamp`; `.get` defaults it to None (backward-compatible).
    ts = ref.get("timestamp")
    return FhirReference(
        resource_type=ResourceType(ref["resource_type"]),
        resource_id=ref["resource_id"],
        field=ref["field"],
        value=ref["value"],
        last_updated=datetime.fromisoformat(last_upd) if last_upd else None,
        timestamp=datetime.fromisoformat(ts) if ts else None,
    )


def _claim_to_json(c: Claim) -> dict[str, Any]:
    return {
        "text": c.text,
        # Record-grounded chart-summary classifications; None when absent
        # (non-observation claims). Serialized as the enum's string value.
        "severity": c.severity.value if c.severity is not None else None,
        "trend_direction": c.trend_direction.value if c.trend_direction is not None else None,
        "value_direction": c.value_direction.value if c.value_direction is not None else None,
        "source_ref": _citation_to_json(c.source_ref),
    }


def _claim_from_json(c: dict[str, Any]) -> Claim:
    # Older rows predate `severity`/`trend_direction`/`value_direction`; `.get`
    # defaults to None so a pre-classification memory file deserializes unchanged.
    severity = c.get("severity")
    trend = c.get("trend_direction")
    value_dir = c.get("value_direction")
    return Claim(
        text=c["text"],
        severity=ClaimSeverity(severity) if severity else None,
        trend_direction=TrendDirection(trend) if trend else None,
        value_direction=ValueDirection(value_dir) if value_dir else None,
        # `Claim.source_ref` is the `Citation` union, so whichever variant the
        # discriminator selected round-trips as itself — no cast, no narrowing.
        source_ref=_citation_from_json(c["source_ref"]),
    )


def _summary_to_json(s: MemoryFileSummary) -> dict[str, Any]:
    return {
        "patient_id": s.patient_id.value,
        "claims": [_claim_to_json(c) for c in s.claims],
        "changes": [_claim_to_json(c) for c in s.changes],
    }


def _row_to_summary(row: MemoryFileRow) -> MemoryFileSummary:
    return MemoryFileSummary(
        patient_id=PatientId(value=row.patient_id),
        claims=[_claim_from_json(c) for c in row.summary.get("claims", [])],
        # Older rows predate `changes`; default to none.
        changes=[_claim_from_json(c) for c in row.summary.get("changes", [])],
        acuity_score=row.acuity_score,
        rank_reason=row.rank_reason,
        synthesized_at=row.synthesized_at,
        source_watermark=row.source_watermark,
        content_hash=row.content_hash,
    )
