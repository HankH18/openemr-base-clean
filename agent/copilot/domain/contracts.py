"""Public contracts for the API + tool interfaces.

Every clinical claim carries a `Citation` — the discriminated union of a
`FhirReference` (re-checked against a live re-fetch by ID), a
`DocumentCitation` (re-checked against its stored `extracted_fact`), or a
`GuidelineCitation` (re-checked against its stored chunk). Whichever variant,
the serialized citation exposes the five machine-readable spec keys
`{source_type, source_id, page_or_section, field_or_chunk_id, quote_or_value}`.
The verification layer consumes these and drops anything it cannot re-ground.
See `ARCHITECTURE.md` §"Interfaces & contracts".
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

from copilot.domain.primitives import Citation, FhirReference, PatientId


class ClaimSeverity(StrEnum):
    """Record-grounded severity of an observation claim.

    Derived from the observation's own abnormal flag ('' → ``normal``,
    ``high``/``low`` → ``warning``, ``vhigh``/``vlow``/``HH``/``LL`` →
    ``critical``). Never inferred from a range the agent invented. Absent on
    non-observation claims (medications, conditions, allergies) and on any
    claim whose flag cannot be read.
    """

    normal = "normal"
    warning = "warning"
    critical = "critical"


class TrendDirection(StrEnum):
    """Whether the latest reading is moving toward or away from its range.

    Computed from the distance-to-range of the latest vs the prior reading:
    entering the band or shrinking the distance is ``improving``, leaving it or
    growing the distance is ``worsening``, and both-in-range / no-change is
    ``steady``. Grounded in the record's values + the record/standard range;
    ``None`` when it cannot be judged (no prior reading, non-numeric, or no
    range at all) so the UI stays neutral.
    """

    improving = "improving"
    worsening = "worsening"
    steady = "steady"


class ValueDirection(StrEnum):
    """Which way the latest reading moved vs the prior one — the value's motion
    over time, independent of the reference range.

    ``up`` when the latest value increased, ``down`` when it decreased, ``none``
    when it was unchanged or there is no prior reading (or either reading is
    non-numeric). Grounded in the record's own successive values. Drives the
    UI's movement arrow (↑ / ↓ / —); its *colour* comes from ``trend_direction``
    (toward the range → green, away → red), so the two are read together. Absent
    on non-observation claims.
    """

    up = "up"
    down = "down"
    none = "none"


class Claim(BaseModel):
    """One assertion inside a memory file or a chat answer.

    A claim without a valid `source_ref` cannot pass verification — that's
    the fail-closed rule.  `text` is what the LLM wrote; verification
    compares `source_ref.value` against `text` for numeric/med-name exact
    match.

    `source_ref` is the citation discriminated union `Citation`
    (`FhirReference | DocumentCitation | GuidelineCitation`, keyed on
    `source_type`) — the real type, so a document- or guideline-cited claim is
    *expressible* and type-checks. It was previously narrowed to
    `SkipValidation[FhirReference]`, which let readers dereference
    `.value` / `.resource_type` unguarded but meant no producer could construct
    a non-fhir claim: the spec's document/guideline citation variants were
    unreachable in practice. Every reader now isinstance-narrows before touching
    a variant-specific field, so the union is honest at both ends.

    Each variant is grounded by the deterministic verifier against its own
    store — fhir against a live re-fetch, document against its stored
    `extracted_fact`, guideline against its stored chunk (see
    `verification.core`) — and a citation whose source cannot be
    re-materialized still fails attribution and is dropped, fail-closed.

    `severity`, `trend_direction`, and `value_direction` are optional,
    record-grounded classifications attached to observation claims by the
    chart-summary builder (see `rounds.summary`). They are presentation hints
    only — never part of the value-match gate — so a `None`/absent
    classification leaves an existing claim and its verification wholly
    unaffected.
    """

    model_config = ConfigDict(frozen=True)

    text: str = Field(min_length=1)
    source_ref: Citation
    severity: ClaimSeverity | None = None
    trend_direction: TrendDirection | None = None
    value_direction: ValueDirection | None = None


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
    changes: list[Claim] = Field(default_factory=list)
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


# --- Observation time-series (drill-down) -----------------------------------


class ReferenceRange(BaseModel):
    """A metric's numeric reference bounds, each independently optional.

    Parsed from an Observation's ``referenceRange[0]`` — ``null`` on the wire
    when neither bound is derivable, so the chart never invents a band.
    """

    model_config = ConfigDict(frozen=True)

    low: float | None = None
    high: float | None = None

    @model_validator(mode="before")
    @classmethod
    def _order_bounds(cls, data: Any) -> Any:
        """Swap an inverted band so ``low <= high`` always holds.

        Defense-in-depth beside :func:`copilot.rounds.ranges.reference_bounds`:
        an inverted structured ``referenceRange`` (``low > high``) must never
        reach the chart or ``_distance_to_range`` as a backwards band. Runs
        ``before`` field validation on the raw input so the frozen instance is
        built already-ordered — no post-construction mutation.
        """
        if isinstance(data, dict):
            low = data.get("low")
            high = data.get("high")
            if (
                isinstance(low, (int, float))
                and not isinstance(low, bool)
                and isinstance(high, (int, float))
                and not isinstance(high, bool)
                and low > high
            ):
                return {**data, "low": high, "high": low}
        return data


class ObservationSeriesPoint(BaseModel):
    """One grounded reading in a metric time-series.

    Each point is independently auditable — ``resource_id`` locates the exact
    Observation, ``value`` is the verbatim source string (same discipline as a
    claim), and ``timestamp`` is the raw ISO instant from ``extract_temporal``
    (``effectiveDateTime`` → ``issued``). A point that cannot ground a value or
    a timestamp is dropped upstream, never fabricated.
    """

    model_config = ConfigDict(frozen=True)

    resource_id: str = Field(min_length=1)
    value: str = Field(description="Verbatim numeric value as a string, straight from source.")
    timestamp: str = Field(min_length=1, description="Clinical instant, verbatim ISO string.")
    abnormal: str = Field(
        default="",
        description="Observation interpretation / OpenEMR abnormal flag; '' when normal/absent.",
    )


class ObservationSeries(BaseModel):
    """A patient's readings for one metric, oldest→newest, each grounded.

    Orthogonal to the verified-claim contract: a lazily-fetched, patient-scoped
    series feeding a drill-down chart. An unknown/absent metric yields an empty
    ``points`` list (fail-closed), never a fabricated series.
    """

    model_config = ConfigDict(frozen=True)

    patient_id: int = Field(gt=0)
    metric: str
    unit: str = ""
    reference_range: ReferenceRange | None = None
    points: list[ObservationSeriesPoint] = Field(default_factory=list)


# --- Verification -----------------------------------------------------------


class VerificationAction(StrEnum):
    served = "served"
    withheld = "withheld"
    degraded = "degraded"


class VerificationClaimResult(BaseModel):
    """Per-claim outcome from the deterministic gate.

    ``source_ref`` mirrors ``Claim.source_ref`` — the citation union carried
    through verbatim, so a result reports the same citation the claim asserted,
    whichever variant it is. A citation whose source could not be
    re-materialized surfaces here as a failed result (``attribution_ok=False``)
    so the caller can report it dropped.
    """

    model_config = ConfigDict(frozen=True)

    text: str
    source_ref: Citation
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
    """One dependency's status inside `/ready`.

    Graded, not boolean: alongside the ``ok`` gate, a derived ``status`` string
    distinguishes ``ok`` (serving), ``degraded`` (a non-gating/advisory
    dependency that is down but does not pull the service out of rotation), and
    ``down`` (a gating dependency that is unreachable). A graded readiness lets a
    dashboard tell "running in a reduced mode" apart from "not ready".
    """

    model_config = ConfigDict(frozen=True)

    name: str
    ok: bool
    detail: str = ""
    advisory: bool = Field(
        default=False,
        description=(
            "Advisory dependencies are reported for visibility but do not gate "
            "readiness — a failing advisory dep never turns /ready into 503."
        ),
    )

    @computed_field  # type: ignore[prop-decorator]  # mypy limitation: property under a decorator
    @property
    def status(self) -> str:
        """Derived grade: ``ok`` when healthy, ``degraded`` when a failing advisory
        dep, ``down`` when a failing gating dep."""
        if self.ok:
            return "ok"
        return "degraded" if self.advisory else "down"


class ReadinessResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    ready: bool
    dependencies: list[ReadinessDependency]

    @classmethod
    def from_dependencies(cls, dependencies: list[ReadinessDependency]) -> ReadinessResponse:
        """Aggregate dependency results into a response.

        Readiness is the conjunction of every *gating* dependency; advisory
        dependencies (e.g. observability) are surfaced in the payload but never
        block readiness.
        """
        ready = all(d.ok for d in dependencies if not d.advisory)
        return cls(ready=ready, dependencies=dependencies)

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
