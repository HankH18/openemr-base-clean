"""Rounding-session orchestration.

Keeps the route thin. One place fetches each listed patient's FHIR resources,
computes a deterministic acuity score, synthesizes a grounded memory file
carrying that score, persists both the memory files and the clinician's
rounding cursor, and builds the :class:`PatientCard` the UI shows one patient
at a time.

The cursor is the durable position: ``start``/``advance`` write it through
:class:`MemoryRepository`, and ``current``/``advance`` read it back — so a
fresh process resumes exactly where the last one left off.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, ConfigDict

from copilot.config import Settings
from copilot.domain.contracts import MemoryFileSummary, PatientCard, PatientCardFreshness
from copilot.domain.primitives import ClinicianId, PatientId, ResourceType, utcnow
from copilot.fhir.client import FhirClient
from copilot.fhir.provider import build_fhir_client
from copilot.memory.db import session_scope
from copilot.memory.repository import MemoryRepository
from copilot.observability import current_correlation_id
from copilot.rounds.ranking import AcuityAssessment, assess_patient, rank
from copilot.rounds.summary import build_change_claims, build_summary_claims
from copilot.worker.synthesizer import StubSynthesizer, SynthesisInput

_logger = logging.getLogger(__name__)

# Resource types pulled to ground each card. Observations drive acuity; the
# rest add reconciliation context (problems / meds / allergies) as grounded
# claims. Deliberate subset — matches what the memory file actually reads.
_FETCH_TYPES: tuple[ResourceType, ...] = (
    ResourceType.Observation,
    ResourceType.Condition,
    ResourceType.MedicationRequest,
    ResourceType.AllergyIntolerance,
)

# A card is "stale" once it is older than this at serve time. Memory is a cache
# over OpenEMR; past this window the UI should flag that the summary may lag.
_STALE_AFTER = timedelta(minutes=30)


class NoActiveRoundError(Exception):
    """Raised when a clinician has no established rounding session."""


class RoundView(BaseModel):
    """One step of the round: the current card plus the full acuity order."""

    model_config = ConfigDict(frozen=True)

    current: PatientCard
    order: list[int]


class RoundsService:
    """Serve-time orchestration for the rounding loop."""

    def __init__(
        self,
        settings: Settings,
        *,
        fhir_client_factory: Callable[[], FhirClient] | None = None,
    ) -> None:
        self._settings = settings
        # Optional per-request reader factory. In ``smart`` mode the route injects
        # a factory that builds the physician's delegated per-session client; when
        # absent (disabled mode) the reader falls back to the system-token path.
        # Only ``start`` fetches from FHIR — the cursor-only steps never build one.
        self._fhir_client_factory = fhir_client_factory

    # --- public API -------------------------------------------------------

    async def start(self, clinician_id: ClinicianId, patient_ids: Sequence[PatientId]) -> RoundView:
        """Establish the round: fetch, score, synthesize, persist, then rank.

        Returns the current (highest-acuity) card.
        """
        unique = _dedupe(patient_ids)
        if not unique:
            raise ValueError("patient_ids must be non-empty")

        summaries: dict[int, MemoryFileSummary] = {}
        assessments: list[AcuityAssessment] = []
        async with self._fhir_client() as fhir:
            synth = StubSynthesizer()
            for pid in unique:
                resources = await _fetch_resources(fhir, pid)
                assessment = assess_patient(pid, resources)
                assessments.append(assessment)
                summaries[pid.value] = await _synthesize(synth, pid, resources, assessment)

        ordered = [a.patient_id.value for a in rank(assessments)]

        async with session_scope() as session:
            repo = MemoryRepository(session)
            for summary in summaries.values():
                await repo.save_memory_file(summary)
            await repo.upsert_rounding_cursor(clinician_id, ordered, 0, [])

        # HIPAA §164.312(b): one access-trail row per chart this round read.
        await self._record_reads_audit(
            action="rounds.start", clinician_id=clinician_id, patient_ids=unique
        )

        return RoundView(current=_card_from_summary(summaries[ordered[0]]), order=ordered)

    async def current(self, clinician_id: ClinicianId) -> RoundView:
        """Return the clinician's current card; raise if no active round."""
        async with session_scope() as session:
            repo = MemoryRepository(session)
            cursor = await repo.get_rounding_cursor(clinician_id)
            if cursor is None or not cursor.ordered_patient_ids:
                raise NoActiveRoundError
            view = await _view_at(repo, cursor.ordered_patient_ids, cursor.current_index)
        if view is None:
            raise NoActiveRoundError
        await self._record_reads_audit(
            action="rounds.current",
            clinician_id=clinician_id,
            patient_ids=[view.current.patient_id],
        )
        return view

    async def advance(self, clinician_id: ClinicianId, completed: PatientId) -> RoundView | None:
        """Mark ``completed`` seen, move the cursor on, return the next card.

        Returns ``None`` when the list is exhausted (the caller reports done).
        Raises :class:`NoActiveRoundError` when there is no session to advance.
        """
        async with session_scope() as session:
            repo = MemoryRepository(session)
            cursor = await repo.get_rounding_cursor(clinician_id)
            if cursor is None or not cursor.ordered_patient_ids:
                raise NoActiveRoundError
            await repo.set_last_seen(clinician_id, completed)
            completed_ids = list(cursor.completed_ids)
            if completed.value not in completed_ids:
                completed_ids.append(completed.value)
            next_index = cursor.current_index + 1
            await repo.upsert_rounding_cursor(
                clinician_id, list(cursor.ordered_patient_ids), next_index, completed_ids
            )
            view = await _view_at(repo, cursor.ordered_patient_ids, next_index)
        if view is not None:
            await self._record_reads_audit(
                action="rounds.advance",
                clinician_id=clinician_id,
                patient_ids=[view.current.patient_id],
            )
        return view

    async def jump(self, clinician_id: ClinicianId, target: PatientId) -> RoundView:
        """Move the cursor to ``target`` (already on the list); return its card.

        A jump reuses the summaries synthesized at ``start`` — it only
        repositions the durable cursor, so it is instant and lands exactly on
        the requested patient (no re-ranking, no re-synthesis). Raises
        :class:`NoActiveRoundError` when there is no session or the patient is
        not on the established list.
        """
        async with session_scope() as session:
            repo = MemoryRepository(session)
            cursor = await repo.get_rounding_cursor(clinician_id)
            if cursor is None or not cursor.ordered_patient_ids:
                raise NoActiveRoundError
            ordered = list(cursor.ordered_patient_ids)
            if target.value not in ordered:
                raise NoActiveRoundError
            index = ordered.index(target.value)
            await repo.upsert_rounding_cursor(
                clinician_id, ordered, index, list(cursor.completed_ids)
            )
            view = await _view_at(repo, ordered, index)
        if view is None:
            raise NoActiveRoundError
        await self._record_reads_audit(
            action="rounds.jump",
            clinician_id=clinician_id,
            patient_ids=[view.current.patient_id],
        )
        return view

    # --- collaborators ----------------------------------------------------

    async def _record_reads_audit(
        self,
        *,
        action: str,
        clinician_id: ClinicianId,
        patient_ids: Sequence[PatientId],
    ) -> None:
        """Append one HIPAA access-trail row per patient chart read.

        Fail-open: the card is already served, so a failed audit write must
        never turn a round step into an error. All rows for one step share a
        single transaction; any failure is logged and swallowed.
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
                "failed to write rounds read audit rows",
                extra={"action": action, "clinician_id": clinician_id.value},
            )

    def _fhir_client(self) -> FhirClient:
        """Build the FHIR reader for a rounding synthesis.

        Smart mode: the route-injected factory builds the physician's delegated
        per-session client, so OpenEMR attributes the read to that physician.
        Otherwise (disabled mode): the environment-appropriate system client —
        real Backend Services token when configured, else a stub bearer (see
        ``copilot.fhir.provider.build_token_provider``).
        """
        if self._fhir_client_factory is not None:
            return self._fhir_client_factory()
        return build_fhir_client(self._settings)


# --- module helpers --------------------------------------------------------


def _dedupe(patient_ids: Sequence[PatientId]) -> list[PatientId]:
    """Drop duplicate patient ids while preserving first-seen order."""
    seen: set[int] = set()
    out: list[PatientId] = []
    for pid in patient_ids:
        if pid.value not in seen:
            seen.add(pid.value)
            out.append(pid)
    return out


async def _fetch_resources(fhir: FhirClient, pid: PatientId) -> list[Mapping[str, object]]:
    """Pull the round-relevant resources for one patient into a flat list."""
    resources: list[Mapping[str, object]] = []
    for rtype in _FETCH_TYPES:
        bundle = await fhir.search(rtype, {"patient": str(pid)})
        for entry in bundle.get("entry", []) or []:
            res = entry.get("resource")
            if isinstance(res, dict):
                resources.append(res)
    return resources


async def _synthesize(
    synth: StubSynthesizer,
    pid: PatientId,
    resources: Sequence[Mapping[str, object]],
    assessment: AcuityAssessment,
) -> MemoryFileSummary:
    """Synthesize a grounded summary and stamp the computed acuity onto it.

    The stub sets a placeholder ``acuity_score`` of 0.0; the deterministic
    ranking is the source of truth, so it overrides both acuity fields.
    """
    proposed = await synth.synthesize(
        SynthesisInput(
            patient_id=pid,
            resources=resources,
            source_watermark=_watermark(resources),
        )
    )
    return proposed.model_copy(
        update={
            "acuity_score": assessment.acuity_score,
            "rank_reason": assessment.rank_reason,
            # One row per metric (latest reading + trend), not a dateless list
            # of every reading — see copilot.rounds.summary.
            "claims": build_summary_claims(resources),
            # What moved/turned abnormal since the last (~12h ago) visit.
            "changes": build_change_claims(resources),
        }
    )


async def _view_at(repo: MemoryRepository, ordered: Sequence[int], index: int) -> RoundView | None:
    """Build the view for ``ordered[index]``, or ``None`` if out of range/missing."""
    if index < 0 or index >= len(ordered):
        return None
    summary = await repo.get_memory_file(PatientId(value=ordered[index]))
    if summary is None:
        return None
    return RoundView(current=_card_from_summary(summary), order=list(ordered))


def _card_from_summary(summary: MemoryFileSummary) -> PatientCard:
    """Turn a persisted (or freshly built) summary into a UI card."""
    as_of = summary.synthesized_at
    if as_of.tzinfo is None:
        # SQLite drops tz info; treat naive stored timestamps as UTC.
        as_of = as_of.replace(tzinfo=UTC)
    age_seconds = max(0, int((utcnow() - as_of).total_seconds()))
    return PatientCard(
        patient_id=summary.patient_id,
        summary_claims=summary.claims,
        changes_since_last_seen=summary.changes,
        acuity_score=summary.acuity_score,
        rank_reason=summary.rank_reason,
        freshness=PatientCardFreshness(
            as_of=as_of,
            age_seconds=age_seconds,
            stale=age_seconds > int(_STALE_AFTER.total_seconds()),
        ),
    )


def _watermark(resources: Sequence[Mapping[str, object]]) -> datetime:
    """Highest ``meta.lastUpdated`` across the pulled set, else now.

    Real FHIR data mixes tz-aware stamps (``...Z``) with naive ones; comparing
    the two raises ``TypeError`` (not ``ValueError``). Each parsed stamp is
    normalized to UTC when naive before comparison — mirroring
    :func:`copilot.rounds.summary._parse` — so a naive stamp beside an aware one
    can never propagate a 500 out of :meth:`RoundsService.start`.
    """
    best: datetime | None = None
    for res in resources:
        meta = res.get("meta")
        raw = meta.get("lastUpdated") if isinstance(meta, Mapping) else None
        if not isinstance(raw, str):
            continue
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        dt = dt if dt.tzinfo else dt.replace(tzinfo=UTC)
        if best is None or dt > best:
            best = dt
    return best or utcnow()
