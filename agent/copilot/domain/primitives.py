"""Typed domain primitives — reject bad IDs at construction time.

`PatientId` and `ClinicianId` are wrappers around ints so PHPStan-style
argument-transposition bugs are impossible.  `FhirReference` is the
one-and-only way we point at a source resource inside memory files and
verification results.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal

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


class CitationSourceType(StrEnum):
    """Which grounding surface a claim's citation points at.

    The discriminator of the ``Citation`` union: ``fhir`` is the Week-1
    live-record citation (:class:`FhirReference`); ``document`` and
    ``guideline`` are the Week-2 additions (an extracted document fact with
    pixel-level provenance, and a retrieved guideline chunk). Persisted to
    the memory-file JSON, so it is a backed StrEnum. A row with no
    ``source_type`` predates Week-2 and rehydrates as ``fhir``.
    """

    fhir = "fhir"
    document = "document"
    guideline = "guideline"


class PatientId(BaseModel):
    """OpenEMR patient PID — positive integer only."""

    model_config = ConfigDict(frozen=True)

    value: int = Field(gt=0)

    def __str__(self) -> str:
        return str(self.value)


class ClinicianId(BaseModel):
    """OpenEMR user id — positive integer only."""

    model_config = ConfigDict(frozen=True)

    value: int = Field(gt=0)

    def __str__(self) -> str:
        return str(self.value)


CorrelationId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_\-]+$"
    ),
]
"""Per-request/tick trace ID. Threaded through logs + Langfuse."""


class FhirReference(BaseModel):
    """Structured source pointer — the ``fhir`` variant of the citation union.

    ``resource_type`` + ``resource_id`` locate the record; ``last_updated``
    is the resource's own ``meta.lastUpdated`` when the claim was made — the
    verification layer compares against a live re-fetch to detect drift.

    ``field`` and ``value`` are the extracted (path, value) pair the claim
    is asserting — those are what the deterministic numeric-match gate
    compares against.

    ``source_type`` is the union discriminator, fixed to ``fhir``; a persisted
    Week-1 claim carries no ``source_type`` and rehydrates to this default, so
    the migration is byte-compatible.
    """

    model_config = ConfigDict(frozen=True)

    source_type: Literal[CitationSourceType.fhir] = CitationSourceType.fhir
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
    timestamp: datetime | None = Field(
        default=None,
        description=(
            "Clinically meaningful time of the cited resource — authoredOn "
            "(MedicationRequest) or effectiveDateTime (Observation). Grounded, "
            "re-checked on live re-fetch; NOT part of the value-match gate. "
            "Distinct from `last_updated`, which is record-mutation time."
        ),
    )


class DocumentCitation(BaseModel):
    """A claim grounded in an extracted fact from an ingested document.

    The ``document`` variant of the citation union. ``source_id`` is the agent's
    ``source_document`` row id, ``page_or_section`` the 1-based page number,
    ``field_or_chunk_id`` the ``extracted_fact`` row id, and ``quote_or_value``
    the verbatim extracted value. ``bbox`` (normalized ``[x, y, w, h]``) and
    ``confidence`` carry the pixel-level provenance produced by OCR
    reconciliation — the "no-invention" evidence a later grounding pass
    (task F5) re-checks. They are optional so a citation can be minted before
    reconciliation lands.
    """

    model_config = ConfigDict(frozen=True)

    source_type: Literal[CitationSourceType.document] = CitationSourceType.document
    source_id: str = Field(min_length=1, description="source_document row id, as a string.")
    page_or_section: int = Field(ge=1, description="1-based page number the fact was found on.")
    field_or_chunk_id: str = Field(min_length=1, description="extracted_fact row id, as a string.")
    quote_or_value: str = Field(description="Verbatim extracted value, straight from the document.")
    bbox: list[float] | None = Field(
        default=None, description="Normalized [x, y, w, h] of the reconciled OCR span."
    )
    confidence: float | None = Field(
        default=None, description="OCR-reconciliation match confidence in [0, 1]."
    )


class GuidelineCitation(BaseModel):
    """A claim grounded in a retrieved clinical-guideline chunk.

    The ``guideline`` variant of the citation union. ``source_id`` is the
    ``guideline_document`` row id, ``page_or_section`` the section label,
    ``field_or_chunk_id`` the ``guideline_chunk`` row id, and ``quote_or_value``
    the verbatim quoted span the claim leans on.
    """

    model_config = ConfigDict(frozen=True)

    source_type: Literal[CitationSourceType.guideline] = CitationSourceType.guideline
    source_id: str = Field(min_length=1, description="guideline_document row id, as a string.")
    page_or_section: str = Field(min_length=1, description="Section label within the guideline.")
    field_or_chunk_id: str = Field(min_length=1, description="guideline_chunk row id, as a string.")
    quote_or_value: str = Field(description="Verbatim quoted span from the guideline chunk.")


Citation = Annotated[
    FhirReference | DocumentCitation | GuidelineCitation,
    Field(discriminator="source_type"),
]
"""The claim-citation discriminated union, keyed on ``source_type``.

``fhir`` (:class:`FhirReference`) is the live-record citation the verifier
grounds today; ``document`` and ``guideline`` are the Week-2 variants that a
later grounding pass (task F5) will verify. Pydantic dispatches on the
``source_type`` discriminator, so raw model output validates straight into the
right concrete type. A persisted claim with no ``source_type`` predates the
union and is rehydrated as the ``fhir`` variant by the memory repository.
"""
