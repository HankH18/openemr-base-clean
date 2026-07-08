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
from copilot.domain.primitives import FhirReference, PatientId, ResourceType, utcnow
from copilot.memory.models import AuditLogRow, MemoryFileRow, SyncStateRow


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


# --- (de)serialization ------------------------------------------------------


def _summary_to_json(s: MemoryFileSummary) -> dict[str, Any]:
    return {
        "patient_id": s.patient_id.value,
        "claims": [
            {
                "text": c.text,
                "source_ref": {
                    "resource_type": c.source_ref.resource_type.value,
                    "resource_id": c.source_ref.resource_id,
                    "field": c.source_ref.field,
                    "value": c.source_ref.value,
                    "last_updated": c.source_ref.last_updated.isoformat()
                    if c.source_ref.last_updated
                    else None,
                },
            }
            for c in s.claims
        ],
    }


def _row_to_summary(row: MemoryFileRow) -> MemoryFileSummary:
    claims: list[Claim] = []
    for c in row.summary.get("claims", []):
        ref = c["source_ref"]
        last_upd = ref.get("last_updated")
        claims.append(
            Claim(
                text=c["text"],
                source_ref=FhirReference(
                    resource_type=ResourceType(ref["resource_type"]),
                    resource_id=ref["resource_id"],
                    field=ref["field"],
                    value=ref["value"],
                    last_updated=datetime.fromisoformat(last_upd) if last_upd else None,
                ),
            )
        )
    return MemoryFileSummary(
        patient_id=PatientId(value=row.patient_id),
        claims=claims,
        acuity_score=row.acuity_score,
        rank_reason=row.rank_reason,
        synthesized_at=row.synthesized_at,
        source_watermark=row.source_watermark,
        content_hash=row.content_hash,
    )
