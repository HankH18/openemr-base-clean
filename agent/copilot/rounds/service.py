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

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, ConfigDict

from copilot.config import Settings
from copilot.domain.contracts import MemoryFileSummary, PatientCard, PatientCardFreshness
from copilot.domain.primitives import ClinicianId, PatientId, ResourceType, utcnow
from copilot.fhir.client import FhirClient
from copilot.fhir.provider import build_fhir_client
from copilot.memory.db import session_scope
from copilot.memory.repository import MemoryRepository
from copilot.rounds.ranking import AcuityAssessment, assess_patient, rank
from copilot.rounds.summary import build_summary_claims
from copilot.worker.synthesizer import StubSynthesizer, SynthesisInput

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

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

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
            return await _view_at(repo, cursor.ordered_patient_ids, next_index)

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
        return view

    # --- collaborators ----------------------------------------------------

    def _fhir_client(self) -> FhirClient:
        """Build the FHIR reader for a rounding synthesis.

        Real Backend Services token when configured, else a stub bearer — see
        ``copilot.fhir.provider.build_token_provider``.
        """
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
        changes_since_last_seen=[],
        acuity_score=summary.acuity_score,
        rank_reason=summary.rank_reason,
        freshness=PatientCardFreshness(
            as_of=as_of,
            age_seconds=age_seconds,
            stale=age_seconds > int(_STALE_AFTER.total_seconds()),
        ),
    )


def _watermark(resources: Sequence[Mapping[str, object]]) -> datetime:
    """Highest ``meta.lastUpdated`` across the pulled set, else now."""
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
        if best is None or dt > best:
            best = dt
    return best or utcnow()
