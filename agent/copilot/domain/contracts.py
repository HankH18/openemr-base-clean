"""Public contracts for the API + tool interfaces.

Every clinical value carries a `FhirReference`.  The verification layer
consumes these and compares against a live re-fetch by ID.  See
`ARCHITECTURE.md` §"Interfaces & contracts".
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from copilot.domain.primitives import FhirReference, PatientId


class Claim(BaseModel):
    """One assertion inside a memory file or a chat answer.

    A claim without a valid `source_ref` cannot pass verification — that's
    the fail-closed rule.  `text` is what the LLM wrote; verification
    compares `source_ref.value` against `text` for numeric/med-name exact
    match.
    """

    model_config = ConfigDict(frozen=True)

    text: str = Field(min_length=1)
    source_ref: FhirReference


class LabResult(BaseModel):
    """One numeric lab result with reference range + abnormal flag.

    Shape matches the fields the agent's domain rules actually key on.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    value: str  # keep as string to preserve source formatting (e.g. "0.02", "<0.04")
    units: str
    range: str
    abnormal: str = Field(
        default="",
        description="'' | 'high' | 'low' | 'critical_high' | 'critical_low' — OpenEMR convention.",
    )
    observed_at: datetime
    source_ref: FhirReference


class MedListItem(BaseModel):
    """One reconciled medication."""

    model_config = ConfigDict(frozen=True)

    name: str
    dosage: str = ""
    route: str = ""
    active: bool = True
    source_ref: FhirReference


class MedicationList(BaseModel):
    """Reconciled meds — `lists` (medication rows) UNION `prescriptions`.

    `conflicts` names the divergences the reconciliation could not resolve
    (name / dose / active differs between the two stores).  These are
    surfaced to the physician, not silently merged — see ARCHITECTURE
    principle #1 (deterministic core, AI at the edges).
    """

    model_config = ConfigDict(frozen=True)

    items: list[MedListItem]
    conflicts: list[str] = Field(default_factory=list)


class MemoryFileSummary(BaseModel):
    """The persisted per-patient summary.

    Regenerable at any time — memory is a cache, OpenEMR is the source of
    truth.  `content_hash` gates re-synthesis in the poller.
    """

    model_config = ConfigDict(frozen=True)

    patient_id: PatientId
    claims: list[Claim]
    acuity_score: float = Field(ge=0.0, le=10.0)
    rank_reason: str
    synthesized_at: datetime
    source_watermark: datetime
    content_hash: str = Field(min_length=1)


class PatientCardFreshness(BaseModel):
    model_config = ConfigDict(frozen=True)

    as_of: datetime
    age_seconds: int = Field(ge=0)
    stale: bool


class PatientCard(BaseModel):
    """What the round loop hands to the UI for one patient."""

    model_config = ConfigDict(frozen=True)

    patient_id: PatientId
    summary_claims: list[Claim]
    changes_since_last_seen: list[Claim]
    acuity_score: float
    rank_reason: str
    freshness: PatientCardFreshness


# --- Verification -----------------------------------------------------------


class VerificationAction(StrEnum):
    served = "served"
    withheld = "withheld"
    degraded = "degraded"


class VerificationClaimResult(BaseModel):
    """Per-claim outcome from the deterministic gate."""

    model_config = ConfigDict(frozen=True)

    text: str
    source_ref: FhirReference
    attribution_ok: bool
    value_match: bool
    entailment: bool | None = None
    reason: str = ""


class VerificationDomainFlag(BaseModel):
    """A domain-rule finding (allergy conflict, critical lab, etc.)."""

    model_config = ConfigDict(frozen=True)

    rule: str
    severity: str = Field(description="'info' | 'warning' | 'critical'")
    message: str
    must_surface: bool = True
    evidence: list[FhirReference] = Field(default_factory=list)


class VerificationResult(BaseModel):
    """The shared output of `verification`.

    `action == withheld` means the caller MUST NOT expose any claim — the
    fail-closed default.  `degraded` means some claims passed and the rest
    are dropped; `served` means every claim passed.
    """

    model_config = ConfigDict(frozen=True)

    passed: bool
    claims: list[VerificationClaimResult]
    domain_flags: list[VerificationDomainFlag] = Field(default_factory=list)
    action: VerificationAction


# --- Health / Ready ---------------------------------------------------------


class ReadinessDependency(BaseModel):
    """One dependency's status inside `/ready`."""

    model_config = ConfigDict(frozen=True)

    name: str
    ok: bool
    detail: str = ""


class ReadinessResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    ready: bool
    dependencies: list[ReadinessDependency]

    def to_status_code(self) -> int:
        return 200 if self.ready else 503


class HealthResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    alive: bool = True
    version: str


# --- Raw FHIR search response (only what we actually read) -----------------


class FhirBundleCount(BaseModel):
    """Shape of a `_summary=count` response."""

    model_config = ConfigDict(extra="ignore")

    resource_type: str = Field(alias="resourceType")
    total: int = Field(default=0)
    extra: dict[str, Any] = Field(default_factory=dict)
