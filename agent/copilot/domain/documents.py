"""Strict extraction schemas for Week-2 document ingestion.

These are the *parse-don't-validate* boundary between a vision model's raw
output and the agent's typed world. The schema is the source of truth: a
malformed or partial payload raises a ``pydantic.ValidationError`` and is never
silently coerced into a valid-looking object. In particular every model runs in
Pydantic **strict** mode with ``extra="forbid"`` — a string is never coerced to
a bool/float/list, and an unknown key is a hard error — so a VLM that omits a
required field or emits a wrong-typed one fails loudly instead of producing a
confident-but-wrong extraction.

The extracted values stay verbatim strings (``ExtractedFact.value``): the
reconciliation + verification layers compare against the source, so coercing
``"13.5"`` to a float here would throw away formatting we need to match.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class ExtractedFact(BaseModel):
    """One schema-validated fact pulled from a document, with provenance.

    Mirrors the frozen ``extracted_fact`` Phase-0 columns. ``field_path`` and
    ``value`` are required — a fact with neither is not a fact — and ``value``
    is kept as a verbatim string (never coerced numeric). ``supported`` is the
    no-invention gate: True only when the value was located in the page's OCR
    tokens (``bbox`` + ``match_confidence`` set).
    """

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    field_path: str = Field(min_length=1)
    value: str = Field(description="Verbatim extracted value, as a string — never coerced numeric.")
    unit: str | None = None
    reference_range: str | None = None
    abnormal: str | None = None
    # Vision models return dates as ISO strings (JSON has no datetime), so this one
    # field parses leniently even though the model is strict — otherwise a real
    # extraction with a collection date (a required lab field) fails validation.
    collection_date: datetime | None = Field(default=None, strict=False)
    page_no: int | None = Field(default=None, ge=1)
    bbox: list[float] | None = Field(
        default=None, description="Normalized [x, y, w, h] of the reconciled OCR span."
    )
    match_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    supported: bool = False


class LabReport(BaseModel):
    """Strict schema for a parsed laboratory report (VLM extraction target).

    ``facts`` is required with no default, so an empty payload can never default
    itself into a valid (empty) report — the whole point of a strict extraction
    schema.
    """

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    facts: list[ExtractedFact]
    ordering_provider: str | None = None
    specimen: str | None = None
    collected_at: datetime | None = Field(default=None, strict=False)


class IntakeCategory(StrEnum):
    """Where an intake fact belongs in OpenEMR — one value per record type.

    This is what makes the intake extraction *match OpenEMR's schema*: every
    intake fact is tagged with the OpenEMR record it round-trips to, so the
    ``allergy``/``medication``/``medical_problem`` facts map 1:1 into the
    existing write-back path (all three are ``lists`` rows keyed by ``type``).
    """

    demographic = "demographic"  # -> patient_data (fname/lname/DOB/sex/...)
    chief_complaint = "chief_complaint"  # -> form_encounter.reason
    medication = "medication"  # -> lists type='medication'
    allergy = "allergy"  # -> lists type='allergy'
    medical_problem = "medical_problem"  # -> lists type='medical_problem'
    family_history = "family_history"  # -> history_data


class IntakeFact(ExtractedFact):
    """An :class:`ExtractedFact` tagged with its OpenEMR record type.

    Intake facts additionally carry a required :class:`IntakeCategory` so the
    downstream mapping to an OpenEMR record is typed, not a ``field_path`` string
    heuristic. ``strict=False`` on the enum so it parses the plain string value a
    vision model returns (``"allergy"``) — the model emits JSON, not enum members.
    """

    category: IntakeCategory = Field(strict=False)


class IntakeForm(BaseModel):
    """Strict schema for a parsed patient-intake form (VLM extraction target).

    Like :class:`LabReport`, ``facts`` is required content — a blank form does
    not validate. Each fact is an :class:`IntakeFact` (carries its OpenEMR
    ``category``).
    """

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    facts: list[IntakeFact]
    patient_name: str | None = None
    date_of_birth: str | None = None
    completed_at: datetime | None = Field(default=None, strict=False)
