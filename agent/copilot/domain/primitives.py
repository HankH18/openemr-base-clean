"""Typed domain primitives — reject bad IDs at construction time.

`PatientId` and `ClinicianId` are wrappers around ints so PHPStan-style
argument-transposition bugs are impossible.  `FhirReference` is the
one-and-only way we point at a source resource inside memory files and
verification results.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints


def utcnow() -> datetime:
    """Wall-clock UTC now.  Isolated for future clock-injection testability."""
    return datetime.now(UTC)


class ResourceType(StrEnum):
    """FHIR R4 resource types the agent reads.  Closed set — extend deliberately."""

    Patient = "Patient"
    Encounter = "Encounter"
    Observation = "Observation"
    DiagnosticReport = "DiagnosticReport"
    MedicationRequest = "MedicationRequest"
    MedicationStatement = "MedicationStatement"
    Condition = "Condition"
    AllergyIntolerance = "AllergyIntolerance"
    Practitioner = "Practitioner"


class PatientId(BaseModel):
    """OpenEMR patient PID — positive integer only."""

    model_config = ConfigDict(frozen=True)

    value: int = Field(gt=0)

    def __str__(self) -> str:  # noqa: D401 - convention: id-like objects stringify
        return str(self.value)


class ClinicianId(BaseModel):
    """OpenEMR user id — positive integer only."""

    model_config = ConfigDict(frozen=True)

    value: int = Field(gt=0)

    def __str__(self) -> str:
        return str(self.value)


CorrelationId = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_\-]+$"),
]
"""Per-request/tick trace ID. Threaded through logs + Langfuse."""


class FhirReference(BaseModel):
    """Structured source pointer — attached to every claim.

    ``resource_type`` + ``resource_id`` locate the record; ``last_updated``
    is the resource's own ``meta.lastUpdated`` when the claim was made — the
    verification layer compares against a live re-fetch to detect drift.

    ``field`` and ``value`` are the extracted (path, value) pair the claim
    is asserting — those are what the deterministic numeric-match gate
    compares against.
    """

    model_config = ConfigDict(frozen=True)

    resource_type: ResourceType
    resource_id: str = Field(min_length=1)
    field: str = Field(
        min_length=1,
        description=(
            "FHIRPath-like field the claim was extracted from, e.g. "
            "'valueQuantity.value' on an Observation."
        ),
    )
    value: str = Field(description="The extracted value as a string, verbatim from source.")
    last_updated: datetime | None = Field(
        default=None,
        description="`meta.lastUpdated` of the cited resource at synthesis time.",
    )
