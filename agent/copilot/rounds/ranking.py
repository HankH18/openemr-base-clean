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
from copilot.domain.primitives import PatientId, ResourceType
from copilot.verification.core import build_context_from_resources
from copilot.verification.rules import critical_lab

# Score bands stay strictly separated so severity — not the tie-break — drives
# ordering across bands: critical 8.0-10.0, warning 3.5-6.5 (either side of the
# 7.0 alert threshold), normal 1.0. WITHIN a band the score scales with how many
# abnormal findings there are and how far each sits outside its reference range,
# so two equally-critical patients still get distinct, explainable scores instead
# of a flat 9.0 for everyone.
CRITICAL_BASE, CRITICAL_SPAN = 8.0, 2.0
WARNING_BASE, WARNING_SPAN = 3.5, 3.0
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
            # Critical patients also count their warning findings toward the
            # within-band score, so "3 criticals + a warning" outranks "1 critical".
            acuity_score=_band_score(CRITICAL_BASE, CRITICAL_SPAN, critical + warning, context),
            rank_reason=_reason("Critical", critical),
        )
    if warning:
        return AcuityAssessment(
            patient_id=patient_id,
            acuity_score=_band_score(WARNING_BASE, WARNING_SPAN, warning, context),
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


def _band_score(base: float, span: float, flags: Sequence[VerificationDomainFlag], context: Any) -> float:
    """Position within a severity band from finding count + how far out of range.

    More abnormal findings and larger deviations push the score toward the top of
    the band; the result is rounded to one decimal so distinct clinical pictures
    read as distinct scores rather than a flat band constant.
    """
    n = len(flags)
    count_pos = 1.0 - (0.6**n) if n else 0.0  # 1→.40, 2→.64, 3→.78, 4→.87
    pos = min(1.0, 0.5 * count_pos + 0.5 * _severity(flags, context))
    return round(base + span * pos, 1)


def _severity(flags: Sequence[VerificationDomainFlag], context: Any) -> float:
    """Mean deviation-outside-range across the flagged observations, in [0, 1)."""
    magnitudes: list[float] = []
    for flag in flags:
        for ev in flag.evidence:
            res = context.resources_by_key.get((ResourceType.Observation, ev.resource_id))
            if res is not None and (m := _magnitude(res)) is not None:
                magnitudes.append(m)
    return sum(magnitudes) / len(magnitudes) if magnitudes else 0.5


def _magnitude(res: Mapping[str, Any]) -> float | None:
    """How far a value sits outside its reference range, soft-capped to [0, 1)."""
    q = res.get("valueQuantity")
    if not isinstance(q, Mapping):
        return None
    value = q.get("value")
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    low, high = _ref_bounds(res)
    if low is None and high is None:
        return None  # no range to judge against
    if high is not None and value > high:
        width = (high - low) if (low is not None and high > low) else (abs(high) or 1.0)
        over = (value - high) / width
    elif low is not None and value < low:
        width = (high - low) if (high is not None and high > low) else (abs(low) or 1.0)
        over = (low - value) / width
    else:
        return 0.0  # flagged but within the provided bounds — treat as mild
    return over / (over + 1.0)  # diminishing returns: never reaches 1.0


def _ref_bounds(res: Mapping[str, Any]) -> tuple[float | None, float | None]:
    ranges = res.get("referenceRange")
    if not isinstance(ranges, list) or not ranges or not isinstance(ranges[0], Mapping):
        return (None, None)

    def bound(key: str) -> float | None:
        node = ranges[0].get(key)
        if isinstance(node, Mapping) and isinstance(node.get("value"), (int, float)):
            return float(node["value"])
        return None

    return (bound("low"), bound("high"))
