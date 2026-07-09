"""Deterministic acuity ranking for the rounding list.

Sickest-first ordering is a safety-relevant decision, so it must be
reproducible and interrogable — never an LLM guess. The signal comes straight
from the patient's FHIR Observations, reusing the same critical/warning
classification the verification layer already applies
(:func:`copilot.verification.rules.critical_lab`). That way "why is this
patient first?" always traces back to a concrete, cited finding.

Scoring bands (0-10, matching ``MemoryFileSummary.acuity_score``):

- a CRITICAL flag (interpretation ``HH``/``LL`` or an OpenEMR ``abnormal`` of
  ``critical_high``/``critical_low``)          -> :data:`CRITICAL_SCORE`
- a WARNING flag (``H``/``L``/``high``/``low``) -> :data:`WARNING_SCORE`
- no abnormal finding                           -> :data:`NORMAL_SCORE`

The bands are strictly separated, so a patient with a critical lab always
sorts ahead of a warning-only patient, ahead of a normal one — independent of
the tie-break. Ties break by patient id ascending, so a cohort always ranks
identically.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from copilot.domain.contracts import VerificationDomainFlag
from copilot.domain.primitives import PatientId
from copilot.verification.core import build_context_from_resources
from copilot.verification.rules import critical_lab

# Kept strictly separated (and the critical band above
# ``Settings.acuity_alert_threshold`` default of 7.0) so band membership, not
# the tie-break, decides ordering across severities.
CRITICAL_SCORE = 9.0
WARNING_SCORE = 5.0
NORMAL_SCORE = 1.0


class AcuityAssessment(BaseModel):
    """The acuity verdict for one patient — score plus its grounded reason."""

    model_config = ConfigDict(frozen=True)

    patient_id: PatientId
    acuity_score: float = Field(ge=0.0, le=10.0)
    rank_reason: str = Field(min_length=1)


def assess_patient(
    patient_id: PatientId, resources: Sequence[Mapping[str, Any]]
) -> AcuityAssessment:
    """Score one patient from their fetched FHIR resources.

    Deterministic: the same resources always yield the same score and reason.
    """
    context = build_context_from_resources(resources)
    flags = critical_lab(context)
    critical = [f for f in flags if f.severity == "critical"]
    warning = [f for f in flags if f.severity == "warning"]

    if critical:
        return AcuityAssessment(
            patient_id=patient_id,
            acuity_score=CRITICAL_SCORE,
            rank_reason=_reason("Critical", critical),
        )
    if warning:
        return AcuityAssessment(
            patient_id=patient_id,
            acuity_score=WARNING_SCORE,
            rank_reason=_reason("Abnormal", warning),
        )
    return AcuityAssessment(
        patient_id=patient_id,
        acuity_score=NORMAL_SCORE,
        rank_reason="No abnormal findings on the latest labs.",
    )


def rank(assessments: Sequence[AcuityAssessment]) -> list[AcuityAssessment]:
    """Order assessments sickest-first; ties break by patient id ascending."""
    return sorted(assessments, key=lambda a: (-a.acuity_score, a.patient_id.value))


def _reason(prefix: str, flags: Sequence[VerificationDomainFlag]) -> str:
    """Human-readable reason naming the driving finding(s)."""
    messages = [f.message for f in flags if f.message]
    body = "; ".join(messages) if messages else "abnormal finding"
    return f"{prefix}: {body}"
