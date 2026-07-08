"""Domain rules.

Deterministic checks over the FHIR context that produce
``VerificationDomainFlag`` items.  These are additive to (not part of)
the attribution/value gate — a memory file whose claims all pass may
still carry a critical-lab flag or an allergy/med conflict that MUST
be surfaced.

Two rules ship with the MVP:

- ``allergy_medication_conflict`` — flags active AllergyIntolerance /
  active MedicationRequest pairs where the med's substance is in the
  allergy's class list.  Class map is small and curated.
- ``critical_lab`` — flags Observation resources whose
  ``interpretation`` code (US Core convention: HL7 AbnormalFlags) is
  ``HH``/``LL`` (critical high/low) or whose OpenEMR-side ``abnormal``
  extension is ``critical_high``/``critical_low``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Protocol

from copilot.domain.primitives import FhirReference, ResourceType
from copilot.domain.contracts import VerificationDomainFlag


class DomainRule(Protocol):
    def __call__(self, context: Any) -> list[VerificationDomainFlag]: ...


# --- Allergy/medication conflict ------------------------------------------

# Small curated substance-class map. Kept intentionally short — this is a
# demo-quality guardrail, not a full drug database. Production would delegate
# to OpenEMR's CDR / a First Databank-class terminology service.
_PENICILLINS = {
    "penicillin",
    "amoxicillin",
    "amoxicillin-clavulanate",
    "ampicillin",
    "piperacillin",
    "piperacillin-tazobactam",
    "nafcillin",
    "oxacillin",
    "dicloxacillin",
    "methicillin",
}
_SULFONAMIDES = {
    "sulfamethoxazole",
    "sulfamethoxazole-trimethoprim",
    "tmp-smx",
    "bactrim",
    "sulfadiazine",
    "sulfasalazine",
}
_NSAIDS = {
    "ibuprofen",
    "naproxen",
    "ketorolac",
    "diclofenac",
    "meloxicam",
    "celecoxib",
    "aspirin",
    "indomethacin",
}
_CLASS_MEMBERS: dict[str, frozenset[str]] = {
    "penicillin": frozenset(_PENICILLINS),
    "penicillins": frozenset(_PENICILLINS),
    "sulfa": frozenset(_SULFONAMIDES),
    "sulfa drugs": frozenset(_SULFONAMIDES),
    "sulfonamide": frozenset(_SULFONAMIDES),
    "nsaids": frozenset(_NSAIDS),
    "nsaid": frozenset(_NSAIDS),
}


def _tokens(name: str) -> set[str]:
    """Lower-case, split on non-alpha, drop dosage/units words."""
    import re

    return {t for t in re.findall(r"[a-z]+(?:-[a-z]+)*", name.lower()) if len(t) > 2}


def _display_name(res: Mapping[str, Any]) -> str:
    """Best-effort extract a medication/allergy display name."""
    # US Core MedicationRequest.medicationCodeableConcept.text/coding[0].display
    # AllergyIntolerance.code.text/coding[0].display
    code = res.get("medicationCodeableConcept") or res.get("code") or {}
    if isinstance(code, Mapping):
        text = code.get("text")
        if isinstance(text, str) and text:
            return text
        coding = code.get("coding") or []
        if isinstance(coding, list) and coding:
            first = coding[0]
            if isinstance(first, Mapping):
                display = first.get("display")
                if isinstance(display, str):
                    return display
    # OpenEMR-side: sometimes just `title`
    title = res.get("title")
    if isinstance(title, str):
        return title
    return ""


def _is_active(res: Mapping[str, Any], resource_type: ResourceType) -> bool:
    if resource_type == ResourceType.MedicationRequest:
        # US Core status: active|on-hold|cancelled|completed|entered-in-error|stopped|draft|unknown
        return str(res.get("status", "")).lower() == "active"
    if resource_type == ResourceType.AllergyIntolerance:
        clinical = res.get("clinicalStatus") or {}
        coding = clinical.get("coding") if isinstance(clinical, Mapping) else None
        if isinstance(coding, list) and coding:
            first = coding[0]
            if isinstance(first, Mapping):
                return str(first.get("code", "")).lower() == "active"
        # Fallback for OpenEMR bundle: 'activity' == 1 or missing
        return res.get("activity", 1) == 1
    return True


def _substances_for_allergy(name: str) -> frozenset[str]:
    """Given an allergy display name, expand to the class members.

    "Penicillin" ⇒ full penicillin class.  A specific drug like
    "Amoxicillin" only conflicts with an exact-name active med.
    """
    key = name.strip().lower()
    if key in _CLASS_MEMBERS:
        return _CLASS_MEMBERS[key]
    # tokenize and check each token for a class hit
    hits: set[str] = set()
    for tok in _tokens(name):
        if tok in _CLASS_MEMBERS:
            hits |= _CLASS_MEMBERS[tok]
    if hits:
        return frozenset(hits)
    # Fall back: exact-name only
    return frozenset({key})


def allergy_medication_conflict(context: Any) -> list[VerificationDomainFlag]:
    """Flag any active allergy that conflicts with an active med."""
    flags: list[VerificationDomainFlag] = []
    allergies = []
    meds = []
    for (rtype, rid), res in context.resources_by_key.items():
        if rtype == ResourceType.AllergyIntolerance and _is_active(res, rtype):
            allergies.append((rid, res))
        elif rtype == ResourceType.MedicationRequest and _is_active(res, rtype):
            meds.append((rid, res))

    for a_id, a_res in allergies:
        a_name = _display_name(a_res)
        if not a_name or a_name.lower().startswith("no known"):
            continue
        substances = _substances_for_allergy(a_name)
        for m_id, m_res in meds:
            m_name = _display_name(m_res)
            if not m_name:
                continue
            m_name_lc = m_name.lower()
            # Match if any substance appears as a token in the med name.
            hit = any(sub in m_name_lc for sub in substances)
            if not hit:
                continue
            flags.append(
                VerificationDomainFlag(
                    rule="allergy_medication_conflict",
                    severity="critical",
                    message=(
                        f"Active medication '{m_name}' conflicts with documented "
                        f"allergy to '{a_name}'."
                    ),
                    must_surface=True,
                    evidence=[
                        FhirReference(
                            resource_type=ResourceType.AllergyIntolerance,
                            resource_id=a_id,
                            field="code",
                            value=a_name,
                        ),
                        FhirReference(
                            resource_type=ResourceType.MedicationRequest,
                            resource_id=m_id,
                            field="medicationCodeableConcept",
                            value=m_name,
                        ),
                    ],
                )
            )
    return flags


# --- Critical labs --------------------------------------------------------


_CRITICAL_HIGH = {"HH", "critical_high"}
_CRITICAL_LOW = {"LL", "critical_low"}
_ABNORMAL_HIGH = {"H", "high"}
_ABNORMAL_LOW = {"L", "low"}


def _abnormal_flag_of(res: Mapping[str, Any]) -> str:
    """Extract the abnormal flag from an Observation.

    Prefers ``interpretation[0].coding[0].code`` (US Core convention);
    falls back to a top-level ``abnormal`` (OpenEMR seed convention).
    """
    interp = res.get("interpretation") or []
    if isinstance(interp, list) and interp:
        first = interp[0]
        if isinstance(first, Mapping):
            coding = first.get("coding") or []
            if isinstance(coding, list) and coding:
                c = coding[0]
                if isinstance(c, Mapping):
                    return str(c.get("code", ""))
    raw = res.get("abnormal")
    if isinstance(raw, str):
        return raw
    return ""


def _obs_label(res: Mapping[str, Any]) -> str:
    code = res.get("code") or {}
    if isinstance(code, Mapping):
        text = code.get("text")
        if isinstance(text, str) and text:
            return text
    return res.get("id", "?")


def _obs_value(res: Mapping[str, Any]) -> str:
    q = res.get("valueQuantity") or {}
    if isinstance(q, Mapping) and "value" in q:
        unit = q.get("unit", "")
        return f"{q['value']} {unit}".strip()
    return ""


def critical_lab(context: Any) -> list[VerificationDomainFlag]:
    """Flag every Observation whose abnormal flag says critical."""
    flags: list[VerificationDomainFlag] = []
    for (rtype, rid), res in context.resources_by_key.items():
        if rtype != ResourceType.Observation:
            continue
        code = _abnormal_flag_of(res)
        if code in _CRITICAL_HIGH:
            severity = "critical"
            direction = "critically high"
        elif code in _CRITICAL_LOW:
            severity = "critical"
            direction = "critically low"
        elif code in _ABNORMAL_HIGH:
            severity = "warning"
            direction = "high"
        elif code in _ABNORMAL_LOW:
            severity = "warning"
            direction = "low"
        else:
            continue

        flags.append(
            VerificationDomainFlag(
                rule="critical_lab" if severity == "critical" else "abnormal_lab",
                severity=severity,
                message=f"{_obs_label(res)} is {direction}: {_obs_value(res)}".strip(),
                must_surface=(severity == "critical"),
                evidence=[
                    FhirReference(
                        resource_type=ResourceType.Observation,
                        resource_id=rid,
                        field="interpretation",
                        value=code,
                    )
                ],
            )
        )
    return flags


def default_rules() -> tuple[DomainRule, ...]:
    """The MVP rule set — extendable but load-bearing today."""
    return (allergy_medication_conflict, critical_lab)
