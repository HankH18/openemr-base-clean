"""Change-gated poller.

One tick per active patient:

1. Read the current watermark from `sync_state` (or None → first-ever).
2. For each relevant resource type, ask FHIR `_summary=count` since the
   watermark.  If **every** type returns zero, skip the patient
   (cost-scales-with-change principle).
3. Otherwise, pull the changed resources.
4. Compute a content hash over the pulled set; if the hash equals the
   stored one, we picked up cosmetic-only updates — skip synthesis
   still, but bump ``last_polled_at``.
5. Hand the resources to the synthesizer.  The caller (Scheduler) is
   responsible for running verification on the returned summary before
   persistence; the Poller returns the outcome so verification stays a
   first-class step, never buried inside a bigger method.

The concrete resource-type set is a parameter so tests can shrink it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from copilot.domain.contracts import MemoryFileSummary
from copilot.domain.primitives import PatientId, ResourceType, utcnow
from copilot.fhir.client import FhirClient, FhirClientError
from copilot.memory.repository import MemoryRepository
from copilot.worker.hashing import content_hash_for_resources
from copilot.worker.synthesizer import LlmSynthesizer, SynthesisError, SynthesisInput


class PollerTickOutcome(StrEnum):
    no_change = "no_change"
    hash_unchanged = "hash_unchanged"
    synthesized = "synthesized"
    error = "error"


@dataclass(frozen=True)
class PollerResult:
    """What one patient tick produced."""

    patient_id: PatientId
    outcome: PollerTickOutcome
    changed_counts: dict[str, int]
    memory_file: MemoryFileSummary | None
    error: str | None = None


# The clinical resource types the poller watches.  Deliberate subset:
# these are what the memory file + verification actually read; expanding
# increases cost without value until a new domain rule needs it.
DEFAULT_WATCHED_TYPES: tuple[ResourceType, ...] = (
    ResourceType.Observation,
    ResourceType.DiagnosticReport,
    ResourceType.MedicationRequest,
    ResourceType.Condition,
    ResourceType.AllergyIntolerance,
    ResourceType.Encounter,
)


class Poller:
    """Owns one tick of the change-detection loop."""

    def __init__(
        self,
        *,
        fhir: FhirClient,
        synthesizer: LlmSynthesizer,
        repository: MemoryRepository,
        watched_types: Sequence[ResourceType] = DEFAULT_WATCHED_TYPES,
    ) -> None:
        self._fhir = fhir
        self._synth = synthesizer
        self._repo = repository
        self._watched = tuple(watched_types)

    async def tick(self, patient_id: PatientId) -> PollerResult:
        """Run one full change → maybe-synthesize cycle for one patient."""
        polled_at = utcnow()
        sync = await self._repo.get_sync_state(patient_id)
        prior_watermark: datetime = (
            sync.watermark
            if sync is not None and sync.watermark is not None
            else datetime(1970, 1, 1)  # first-ever poll: anything counts
        )
        # SQLite drops timezone info; the count-query needs a tz-aware value.
        if prior_watermark.tzinfo is None:
            from datetime import UTC

            prior_watermark = prior_watermark.replace(tzinfo=UTC)
        prior_hash: str = sync.content_hash if sync is not None else ""
        failures: int = sync.consecutive_failures if sync is not None else 0

        counts: dict[str, int] = {}
        try:
            for rt in self._watched:
                counts[rt.value] = await self._fhir.count_since(rt, patient_id, prior_watermark)
        except FhirClientError as exc:
            await self._repo.upsert_sync_state(
                patient_id,
                polled_at=polled_at.replace(tzinfo=None),
                success_at=None,
                watermark=None,
                content_hash=prior_hash,
                consecutive_failures=failures + 1,
            )
            return PollerResult(
                patient_id=patient_id,
                outcome=PollerTickOutcome.error,
                changed_counts=counts,
                memory_file=None,
                error=f"count query failed: {exc}",
            )

        if not any(counts.values()):
            await self._repo.upsert_sync_state(
                patient_id,
                polled_at=polled_at.replace(tzinfo=None),
                success_at=polled_at.replace(tzinfo=None),
                watermark=prior_watermark.replace(tzinfo=None),
                content_hash=prior_hash,
                consecutive_failures=0,
            )
            return PollerResult(
                patient_id=patient_id,
                outcome=PollerTickOutcome.no_change,
                changed_counts=counts,
                memory_file=None,
            )

        try:
            resources = await self._pull_changed(patient_id, prior_watermark, counts)
        except FhirClientError as exc:
            await self._repo.upsert_sync_state(
                patient_id,
                polled_at=polled_at.replace(tzinfo=None),
                success_at=None,
                watermark=None,
                content_hash=prior_hash,
                consecutive_failures=failures + 1,
            )
            return PollerResult(
                patient_id=patient_id,
                outcome=PollerTickOutcome.error,
                changed_counts=counts,
                memory_file=None,
                error=f"resource pull failed: {exc}",
            )

        new_hash = content_hash_for_resources(resources)
        if new_hash == prior_hash and prior_hash != "":
            # Content-hash confirms this was a cosmetic update (identical
            # payload, only lastUpdated moved) — skip Claude call.
            new_watermark = _max_last_updated(resources) or prior_watermark
            await self._repo.upsert_sync_state(
                patient_id,
                polled_at=polled_at.replace(tzinfo=None),
                success_at=polled_at.replace(tzinfo=None),
                watermark=new_watermark.replace(tzinfo=None) if new_watermark else None,
                content_hash=prior_hash,
                consecutive_failures=0,
            )
            return PollerResult(
                patient_id=patient_id,
                outcome=PollerTickOutcome.hash_unchanged,
                changed_counts=counts,
                memory_file=None,
            )

        new_watermark = _max_last_updated(resources) or polled_at
        synth_input = SynthesisInput(
            patient_id=patient_id,
            resources=resources,
            source_watermark=new_watermark,
        )
        try:
            summary = await self._synth.synthesize(synth_input)
        except SynthesisError as exc:
            await self._repo.upsert_sync_state(
                patient_id,
                polled_at=polled_at.replace(tzinfo=None),
                success_at=None,
                watermark=None,
                content_hash=prior_hash,
                consecutive_failures=failures + 1,
            )
            return PollerResult(
                patient_id=patient_id,
                outcome=PollerTickOutcome.error,
                changed_counts=counts,
                memory_file=None,
                error=f"synthesis failed: {exc}",
            )

        # Do NOT persist here.  Verification is the next step and it lives
        # outside the Poller (see ARCHITECTURE §"Data flow" — verification
        # runs at synthesis, then persist).  The Scheduler wires them.
        return PollerResult(
            patient_id=patient_id,
            outcome=PollerTickOutcome.synthesized,
            changed_counts=counts,
            memory_file=summary,
        )

    async def _pull_changed(
        self, patient_id: PatientId, since: datetime, counts: dict[str, int]
    ) -> list[dict[str, Any]]:
        """Pull each resource type that had a nonzero count since ``since``."""
        params_base = {
            "patient": str(patient_id),
            "_lastUpdated": f"gt{since.isoformat().replace('+00:00', 'Z')}",
        }
        pulled: list[dict[str, Any]] = []
        for rt in self._watched:
            if counts.get(rt.value, 0) == 0:
                continue
            bundle = await self._fhir.search(rt, params_base)
            for entry in bundle.get("entry", []) or []:
                res = entry.get("resource")
                if isinstance(res, dict):
                    pulled.append(res)
        return pulled


def _max_last_updated(resources: Sequence[dict[str, Any]]) -> datetime | None:
    """Highest `meta.lastUpdated` across the pulled set — the new watermark."""
    best: datetime | None = None
    for res in resources:
        meta = res.get("meta") or {}
        raw = meta.get("lastUpdated")
        if not isinstance(raw, str):
            continue
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if best is None or dt > best:
            best = dt
    return best
