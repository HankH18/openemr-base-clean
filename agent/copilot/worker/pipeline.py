"""Background refresh pipeline + proactive deterioration alerts.

The serve-time counterpart to the (auto-scheduled) poller loop: a clinician
can force a re-sync of their whole rounding list, and the UI can ask which
not-yet-seen patients have crossed into critical territory.

``refresh`` runs one change-gated tick per patient in the clinician's
rounding cursor, reusing the exact same building blocks the scheduler wires
around ``Poller.tick``:

1. ``Poller.tick`` — count-gate → hash-confirm → (maybe) synthesize.  It
   never persists; that is deliberately left to the caller so verification
   sits *between* synthesis and the store.
2. **Verification at synthesis.**  The proposed summary's claims are gated
   against the live FHIR record (:class:`Verifier`); only claims that pass
   attribution + numeric-value match survive into what we persist.  A
   fabricated claim is dropped here, never written.
3. **Acuity via ranking.**  ``assess_patient`` (the same deterministic
   ranking ``RoundsService.start`` uses) stamps the grounded score + reason
   onto the summary, so the persisted card and the rounding order agree.
4. **Persist.**  The grounded summary is saved and ``sync_state`` is
   advanced (watermark + content hash) so a second refresh with no FHIR
   change is a no-op — change-gated and idempotent.

``alerts`` surfaces UC-5: a patient on the clinician's list who has **not**
been advanced to (no ``last_seen`` row) yet whose persisted acuity is at or
above :attr:`Settings.acuity_alert_threshold` is offered as a deterioration
alert — the sepsis/critical-lactate case that would otherwise wait at the
bottom of the list.

Kept out of the route layer so the endpoints stay thin (parse → delegate →
serialise).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict

from copilot.config import Settings
from copilot.domain.contracts import Claim, MemoryFileSummary
from copilot.domain.primitives import ClinicianId, PatientId, utcnow
from copilot.fhir.client import FhirClient
from copilot.fhir.provider import build_fhir_client
from copilot.memory.db import session_scope
from copilot.memory.repository import MemoryRepository
from copilot.observability import current_correlation_id
from copilot.rounds.ranking import assess_patient
from copilot.verification.core import Verifier, build_context_from_resources
from copilot.verification.rules import default_rules
from copilot.worker.poller import DEFAULT_WATCHED_TYPES, Poller, PollerTickOutcome
from copilot.worker.synthesizer import StubSynthesizer

_logger = logging.getLogger(__name__)


class RefreshResult(BaseModel):
    """Per-patient outcome of one refresh tick."""

    model_config = ConfigDict(frozen=True)

    patient_id: PatientId
    outcome: str
    error: str | None = None


class DeteriorationAlert(BaseModel):
    """A not-yet-seen critical patient the clinician should be pulled toward."""

    model_config = ConfigDict(frozen=True)

    patient_id: PatientId
    reason: str
    acuity_score: float


class RefreshPipeline:
    """Serve-time orchestration for the background update loop."""

    def __init__(
        self,
        settings: Settings,
        *,
        fhir_client_factory: Callable[[], FhirClient] | None = None,
    ) -> None:
        self._settings = settings
        # Optional per-request reader factory. ``refresh`` is an INTERACTIVE,
        # clinician-triggered route, so in ``smart`` mode the route injects a
        # factory that builds the physician's delegated per-session client (same
        # seam as ``RoundsService``/``ChatService``); when absent (disabled mode,
        # or the background poller path) the reader falls back to the
        # environment-appropriate system client via ``build_fhir_client``.
        self._fhir_client_factory = fhir_client_factory

    # --- public API -------------------------------------------------------

    async def refresh(self, clinician_id: ClinicianId) -> list[RefreshResult]:
        """Re-sync every patient in the clinician's rounding list.

        Returns one :class:`RefreshResult` per patient. An empty list means
        the clinician has no active round to refresh.
        """
        async with session_scope() as session:
            repo = MemoryRepository(session)
            cursor = await repo.get_rounding_cursor(clinician_id)
            if cursor is None or not cursor.ordered_patient_ids:
                return []
            patient_ids = [PatientId(value=pid) for pid in cursor.ordered_patient_ids]

            results: list[RefreshResult] = []
            async with self._fhir_client() as fhir:
                poller = Poller(
                    fhir=fhir,
                    synthesizer=StubSynthesizer(),
                    repository=repo,
                )
                verifier = Verifier(rules=default_rules())
                for pid in patient_ids:
                    results.append(await self._refresh_patient(pid, fhir, poller, verifier, repo))

        # HIPAA §164.312(b): one access-trail row per patient chart this refresh
        # read. Every patient in the cursor is disclosed to OpenEMR by the tick's
        # change-gate count query (and, on change, the resource pulls), so the
        # access trail must cover the whole list — mirroring rounds/start and the
        # background poller's per-tick read audit. Fail-open: see
        # ``_record_reads_audit``.
        await self._record_reads_audit(
            action="rounds.refresh", clinician_id=clinician_id, patient_ids=patient_ids
        )
        return results

    async def alerts(self, clinician_id: ClinicianId) -> list[DeteriorationAlert]:
        """Offer deterioration alerts for not-yet-seen critical patients.

        A patient qualifies when there is no ``last_seen`` row for
        ``(clinician, patient)`` and their persisted memory-file acuity is at
        or above the configured alert threshold.
        """
        threshold = self._settings.acuity_alert_threshold
        async with session_scope() as session:
            repo = MemoryRepository(session)
            cursor = await repo.get_rounding_cursor(clinician_id)
            if cursor is None or not cursor.ordered_patient_ids:
                return []

            alerts: list[DeteriorationAlert] = []
            for pid_value in cursor.ordered_patient_ids:
                pid = PatientId(value=pid_value)
                if await repo.get_last_seen(clinician_id, pid) is not None:
                    continue  # already rounded on — no longer a surprise
                memory = await repo.get_memory_file(pid)
                if memory is None or memory.acuity_score < threshold:
                    continue
                alerts.append(
                    DeteriorationAlert(
                        patient_id=pid,
                        reason=memory.rank_reason,
                        acuity_score=memory.acuity_score,
                    )
                )

        # HIPAA §164.312(b): each surfaced alert discloses a patient's identity plus
        # their persisted acuity (from that patient's memory file) — a PHI read — so
        # leave one access-trail row per patient this call actually returned, mirroring
        # the sibling reads. Fail-open (see ``_record_reads_audit``); an empty alert
        # list discloses nothing and writes nothing.
        await self._record_reads_audit(
            action="rounds.alerts",
            clinician_id=clinician_id,
            patient_ids=[a.patient_id for a in alerts],
        )
        return alerts

    # --- per-patient tick -------------------------------------------------

    async def _refresh_patient(
        self,
        pid: PatientId,
        fhir: FhirClient,
        poller: Poller,
        verifier: Verifier,
        repo: MemoryRepository,
    ) -> RefreshResult:
        """One patient: tick, and (only on new synthesis) verify + persist."""
        result = await poller.tick(pid)
        if result.outcome is not PollerTickOutcome.synthesized or result.memory_file is None:
            # no_change / hash_unchanged / error: the poller already advanced
            # sync_state (or recorded the failure); nothing new to persist.
            return RefreshResult(patient_id=pid, outcome=result.outcome.value, error=result.error)

        grounded = await self._ground_and_score(pid, result.memory_file, fhir, verifier)
        await self._persist(pid, grounded, repo)
        return RefreshResult(patient_id=pid, outcome=result.outcome.value)

    async def _ground_and_score(
        self,
        pid: PatientId,
        proposed: MemoryFileSummary,
        fhir: FhirClient,
        verifier: Verifier,
    ) -> MemoryFileSummary:
        """Verify the proposed summary against live FHIR and stamp acuity.

        Only claims that pass the deterministic gate survive; the acuity
        score/reason come from the same ranking the round order uses, so the
        persisted card and the list agree.
        """
        resources = await self._fetch_resources(fhir, pid)
        context = build_context_from_resources(resources)
        verification = await verifier.verify_memory_file(proposed, context)
        verified_claims = [
            Claim(text=claim.text, source_ref=claim.source_ref)
            for claim in verification.claims
            if claim.attribution_ok and claim.value_match
        ]
        assessment = assess_patient(pid, resources)
        return proposed.model_copy(
            update={
                "claims": verified_claims,
                "acuity_score": assessment.acuity_score,
                "rank_reason": assessment.rank_reason,
            }
        )

    async def _persist(
        self, pid: PatientId, grounded: MemoryFileSummary, repo: MemoryRepository
    ) -> None:
        """Save the grounded summary and advance the change-gate watermark."""
        now = utcnow().replace(tzinfo=None)
        await repo.save_memory_file(grounded)
        await repo.upsert_sync_state(
            pid,
            polled_at=now,
            success_at=now,
            watermark=grounded.source_watermark.replace(tzinfo=None),
            content_hash=grounded.content_hash,
            consecutive_failures=0,
        )

    # --- collaborators ----------------------------------------------------

    async def _record_reads_audit(
        self,
        *,
        action: str,
        clinician_id: ClinicianId,
        patient_ids: Sequence[PatientId],
    ) -> None:
        """Append one HIPAA access-trail row per patient chart this read touched.

        Shared by :meth:`refresh` (``action="rounds.refresh"``, the whole cursor) and
        :meth:`alerts` (``action="rounds.alerts"``, the patients actually returned).

        Fail-open: the per-patient outcomes are already computed and about to be
        returned, so a failed audit write must never turn a served read into a 500.
        All rows for one read share a single transaction; any failure is logged and
        swallowed — the same discipline as
        :meth:`RoundsService._record_reads_audit` and the poller's per-tick read
        audit in ``worker/runtime.py``.
        """
        if not patient_ids:
            return
        try:
            async with session_scope() as session:
                repo = MemoryRepository(session)
                for pid in patient_ids:
                    await repo.record_audit(
                        correlation_id=current_correlation_id(),
                        action=action,
                        patient_id=pid,
                        clinician_id=clinician_id.value,
                    )
        except Exception:
            _logger.exception(
                "failed to write read audit rows",
                extra={"action": action, "clinician_id": clinician_id.value},
            )

    async def _fetch_resources(self, fhir: FhirClient, pid: PatientId) -> list[dict[str, Any]]:
        """Pull the watched resource set for one patient into a flat list.

        Mirrors what the poller watches so the verification context covers
        every resource type a claim might cite.
        """
        resources: list[dict[str, Any]] = []
        for rtype in DEFAULT_WATCHED_TYPES:
            bundle = await fhir.search(rtype, {"patient": str(pid)})
            for entry in bundle.get("entry", []) or []:
                res = entry.get("resource")
                if isinstance(res, dict):
                    resources.append(res)
        return resources

    def _fhir_client(self) -> FhirClient:
        """Build the FHIR reader for a refresh tick.

        Mirrors :meth:`RoundsService._fhir_client` exactly (interactive read
        path): when the route injects a per-session factory (``smart`` mode), use
        the physician's delegated client so OpenEMR attributes the read to that
        physician and does not 401. Otherwise — disabled mode, or the background
        poller path that constructs the pipeline without a factory — fall back to
        the environment-appropriate system client (real Backend Services token
        when configured, else the keyless stub bearer from
        :func:`copilot.fhir.provider.build_token_provider`).
        """
        if self._fhir_client_factory is not None:
            return self._fhir_client_factory()
        return build_fhir_client(self._settings)
