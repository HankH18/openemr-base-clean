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

from copilot.domain.contracts import Claim, MemoryFileSummary
from copilot.domain.primitives import (
    ClinicianId,
    FhirReference,
    PatientId,
    ResourceType,
    utcnow,
)
from copilot.memory.models import (
    AuditLogRow,
    ConversationRow,
    LastSeenRow,
    MemoryFileRow,
    MessageRow,
    RoundingCursorRow,
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
    ) -> None:
        self._session.add(
            AuditLogRow(
                correlation_id=correlation_id,
                action=action,
                patient_id=patient_id.value if patient_id else None,
                clinician_id=clinician_id,
                resources_returned=resources_returned or [],
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


# --- (de)serialization ------------------------------------------------------


def _claim_to_json(c: Claim) -> dict[str, Any]:
    return {
        "text": c.text,
        "source_ref": {
            "resource_type": c.source_ref.resource_type.value,
            "resource_id": c.source_ref.resource_id,
            "field": c.source_ref.field,
            "value": c.source_ref.value,
            "last_updated": c.source_ref.last_updated.isoformat()
            if c.source_ref.last_updated
            else None,
            "timestamp": c.source_ref.timestamp.isoformat()
            if c.source_ref.timestamp
            else None,
        },
    }


def _claim_from_json(c: dict[str, Any]) -> Claim:
    ref = c["source_ref"]
    last_upd = ref.get("last_updated")
    # Older rows predate `timestamp`; `.get` defaults it to None (backward-compatible).
    ts = ref.get("timestamp")
    return Claim(
        text=c["text"],
        source_ref=FhirReference(
            resource_type=ResourceType(ref["resource_type"]),
            resource_id=ref["resource_id"],
            field=ref["field"],
            value=ref["value"],
            last_updated=datetime.fromisoformat(last_upd) if last_upd else None,
            timestamp=datetime.fromisoformat(ts) if ts else None,
        ),
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
