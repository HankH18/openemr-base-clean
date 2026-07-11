"""Typed contracts for physician write-back — the read-side model, reversed.

Reads flow ``source → grounding → verification → serve``; writes flow
``typed candidate → verification → echo-back → explicit confirm → commit``.
Everything a write touches is a frozen, typed object over a **closed** set of
writable metrics — no free text ever reaches the DB (see
``research/WRITEBACK_PHASE1_PLAN.md`` §3). These DTOs are the write-side
analogue of ``contracts.py``'s ``Claim`` / ``FhirReference``.

Phase 1a is foundation only: these types plus the deterministic verifier
(``verification/writes.py``) and the write client (``fhir/write_client.py``).
The route + service that orchestrate propose→confirm are Phase 1b.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from copilot.domain.primitives import ClinicianId, PatientId


class WritableMetric(StrEnum):
    """The closed set of vital-sign metrics a physician may write.

    Deliberately small: only OpenEMR's Standard-API *vitals* columns are
    directly writable as Observation-like data (labs have no create endpoint —
    see the feasibility brief). Extend only with a matching column mapping in
    ``fhir/write_client.py`` and a plausibility spec in ``verification/writes.py``
    — the exhaustive ``match`` in each will fail to compile until you do.
    """

    heart_rate = "heart_rate"
    spo2 = "spo2"
    systolic_bp = "systolic_bp"
    diastolic_bp = "diastolic_bp"
    respiratory_rate = "respiratory_rate"
    temperature = "temperature"
    weight = "weight"
    height = "height"


class WriteKind(StrEnum):
    """What kind of record a candidate creates."""

    vital = "vital"
    medication = "medication"


class WriteEntryMode(StrEnum):
    """How the value reached the record — the physician-attribution surface.

    ``human_direct`` is Phase 1: the physician typed the value themselves, so an
    out-of-range value is a soft, overridable warning. ``agent_proposed_
    physician_confirmed`` is **reserved** for Phase 2 (the agent drafts, the
    physician confirms); the write verifier already treats it as the strict mode
    that hard-blocks out-of-range values, so enabling Phase 2 is a one-line
    change, not a re-architecture.
    """

    human_direct = "human_direct"
    agent_proposed_physician_confirmed = "agent_proposed_physician_confirmed"


class VitalWrite(BaseModel):
    """A single-metric vital reading, appended as a new vitals form.

    ``value`` is already parsed to a number at the system boundary (parse, don't
    validate): constructing a ``VitalWrite`` with a non-numeric value or an
    unknown metric fails here, which is the first deterministic gate. Semantic
    checks (unit-matches-metric, physiologic plausibility) are the verifier's job.
    """

    model_config = ConfigDict(frozen=True)

    metric: WritableMetric
    value: float
    unit: str = Field(min_length=1, description="Physician-supplied unit; verified against the metric.")


class MedicationWrite(BaseModel):
    """A medication-list entry, appended as a new list row (latest-wins).

    ``title`` is a picked/echoed drug string, never free prose. ``begdate`` is a
    ``YYYY-MM-DD`` string (the Standard API's format); its format is checked
    deterministically by the verifier, not coerced here.
    """

    model_config = ConfigDict(frozen=True)

    title: str = Field(min_length=1)
    begdate: str = Field(min_length=1, description="Start date, YYYY-MM-DD.")
    enddate: str | None = Field(default=None, description="Optional end date, YYYY-MM-DD.")
    diagnosis: str | None = Field(
        default=None, description="Optional '<codetype>:<code>' association."
    )


class WriteCandidate(BaseModel):
    """A parsed, typed write request over the closed writable surface.

    Carries exactly one payload (``vital`` xor ``medication``) matching ``kind``.
    ``idempotency_key`` is client-generated so a retried/double-clicked confirm
    cannot create a duplicate record. ``patient_id`` / ``clinician_id`` scope the
    write; the route enforces ``is_authorized`` before a candidate is trusted.
    """

    model_config = ConfigDict(frozen=True)

    kind: WriteKind
    patient_id: PatientId
    clinician_id: ClinicianId
    idempotency_key: str = Field(min_length=1, max_length=128)
    entry_mode: WriteEntryMode = WriteEntryMode.human_direct
    vital: VitalWrite | None = None
    medication: MedicationWrite | None = None

    @model_validator(mode="after")
    def _exactly_one_payload(self) -> WriteCandidate:
        present = [p for p in (self.vital, self.medication) if p is not None]
        if len(present) != 1:
            raise ValueError("a WriteCandidate carries exactly one payload")
        if self.kind is WriteKind.vital and self.vital is None:
            raise ValueError("kind=vital requires a vital payload")
        if self.kind is WriteKind.medication and self.medication is None:
            raise ValueError("kind=medication requires a medication payload")
        return self


class WriteVerdict(BaseModel):
    """The deterministic verifier's decision for one candidate.

    ``blocked`` is the hard gate — a blocked candidate must never reach commit.
    ``warnings`` are soft and overridable (an out-of-range human-direct value);
    ``errors`` explain a block. Mirrors ``VerificationResult`` on the read side.
    """

    model_config = ConfigDict(frozen=True)

    kind: WriteKind
    metric: WritableMetric | None = None
    blocked: bool
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when the candidate may proceed to an explicit physician confirm."""
        return not self.blocked


class ProposedWrite(BaseModel):
    """The structured echo-back a physician confirms — never agent prose.

    Returned by the propose step: the exact record to be written, its verdict,
    and the explicit "new record dated now" notice. The frontend renders this as
    a confirmation card; a fat-finger is caught here by a human.
    """

    model_config = ConfigDict(frozen=True)

    candidate: WriteCandidate
    verdict: WriteVerdict
    effective_time: str = Field(default="now", description="Clinical time of the write; always 'now'.")
    notice: str = Field(
        default="This creates a NEW record dated now; it does not overwrite prior values.",
    )


class CommittedWrite(BaseModel):
    """Proof of a committed write — returned only on a confirmed 201/200.

    A write whose success could not be confirmed never produces one of these;
    it raises instead (see ``fhir/write_client.py``). ``encounter_id`` is set for
    vitals (which attach to an encounter) and ``None`` for medications.
    """

    model_config = ConfigDict(frozen=True)

    resource_kind: WriteKind
    new_id: str = Field(min_length=1)
    encounter_id: str | None = None
    committed_at: datetime
