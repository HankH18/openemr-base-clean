"""Domain rules.

Deterministic checks over the FHIR context that produce
``VerificationDomainFlag`` items.  These are additive to (not part of)
the attribution/value gate — a memory file whose claims all pass may
still carry a critical-lab flag or an allergy/med conflict that MUST
be surfaced.

The rule set is additive — a memory file whose claims all pass may still
carry any of these findings:

- ``allergy_medication_conflict`` — flags active AllergyIntolerance /
  active MedicationRequest pairs where the med's substance is in the
  allergy's class list.  Class map is small and curated.
- ``critical_lab`` — flags Observation resources whose
  ``interpretation`` code (US Core convention: HL7 AbnormalFlags) is
  ``HH``/``LL`` (critical high/low) or whose OpenEMR-side ``abnormal``
  extension is ``critical_high``/``critical_low``.
- ``reference_range`` — flags Observation resources whose numeric
  ``valueQuantity.value`` falls outside its ``referenceRange`` **even when
  no interpretation code is present** — the gap ``critical_lab`` (which
  keys purely on interpretation codes) structurally misses.  Skips any
  Observation ``critical_lab``/``abnormal_lab`` already covers, so it never
  double-flags the same result.
- ``medication_reconciliation`` — compares the prescribed orders
  (``MedicationRequest``) against the reported/home list
  (``MedicationStatement``) and flags divergences: a drug in one store but
  not the other, or an active/inactive disagreement for the same drug.

All findings are additive: they surface to the physician but never gate the
served/withheld/degraded action (that is decided purely by claim
verification in ``core.py``), and they are independent of the deterministic
acuity ranking (which consumes ``critical_lab`` alone).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from copilot.domain.contracts import VerificationDomainFlag
from copilot.domain.primitives import FhirReference, ResourceType


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
        return bool(res.get("activity", 1) == 1)
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
    obs_id = res.get("id")
    return obs_id if isinstance(obs_id, str) else "?"


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


# --- Reference-range numeric check ----------------------------------------

# The interpretation codes ``critical_lab``/``abnormal_lab`` already act on.
# An Observation resolving to one of these is skipped by ``reference_range``
# so the same result is never flagged twice.
_RECOGNIZED_INTERP = _CRITICAL_HIGH | _CRITICAL_LOW | _ABNORMAL_HIGH | _ABNORMAL_LOW


def _obs_numeric_value(res: Mapping[str, Any]) -> float | None:
    """Return ``valueQuantity.value`` as a float, or None when non-numeric."""
    q = res.get("valueQuantity")
    if isinstance(q, Mapping):
        v = q.get("value")
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
    return None


def _bound_value(node: Any) -> float | None:
    """Read the numeric ``value`` out of a referenceRange low/high element."""
    if isinstance(node, Mapping):
        v = node.get("value")
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
    return None


def _reference_bounds(res: Mapping[str, Any]) -> tuple[float | None, float | None]:
    """Return ``(low, high)`` from the first ``referenceRange`` — each optional."""
    ranges = res.get("referenceRange")
    if not isinstance(ranges, list) or not ranges:
        return (None, None)
    first = ranges[0]
    if not isinstance(first, Mapping):
        return (None, None)
    return (_bound_value(first.get("low")), _bound_value(first.get("high")))


def _fmt(value: float) -> str:
    """Trim a whole-number float ('135.0' -> '135'); leave decimals intact."""
    return str(int(value)) if value == int(value) else str(value)


def _format_range(low: float | None, high: float | None) -> str:
    if low is not None and high is not None:
        return f"{_fmt(low)}-{_fmt(high)}"
    if low is not None:
        return f">= {_fmt(low)}"
    if high is not None:
        return f"<= {_fmt(high)}"
    return "n/a"


def reference_range(context: Any) -> list[VerificationDomainFlag]:
    """Flag Observations whose numeric value is outside its reference range.

    Catches out-of-range results that carry **no** interpretation code — the
    case the interpretation-driven ``critical_lab`` rule cannot see.  Any
    Observation that already resolves to a recognized abnormal/critical
    interpretation is skipped so the same result is never double-flagged.
    Severity is always ``warning`` (mild): escalation to critical stays the
    job of ``critical_lab`` via an explicit ``HH``/``LL`` code.
    """
    flags: list[VerificationDomainFlag] = []
    for (rtype, rid), res in context.resources_by_key.items():
        if rtype != ResourceType.Observation:
            continue
        # Don't duplicate a flag critical_lab/abnormal_lab already raises.
        if _abnormal_flag_of(res) in _RECOGNIZED_INTERP:
            continue
        value = _obs_numeric_value(res)
        if value is None:
            continue
        low, high = _reference_bounds(res)
        if high is not None and value > high:
            direction = "above the reference range"
        elif low is not None and value < low:
            direction = "below the reference range"
        else:
            continue

        flags.append(
            VerificationDomainFlag(
                rule="reference_range",
                severity="warning",
                message=(
                    f"{_obs_label(res)} is {direction}: "
                    f"{_obs_value(res)} (ref {_format_range(low, high)})"
                ).strip(),
                must_surface=False,
                evidence=[
                    FhirReference(
                        resource_type=ResourceType.Observation,
                        resource_id=rid,
                        field="valueQuantity.value",
                        value=str(value),
                    )
                ],
            )
        )
    return flags


# --- Medication reconciliation --------------------------------------------


def _status_active(res: Mapping[str, Any]) -> bool:
    """True when a Medication* resource's ``status`` is ``active``."""
    return str(res.get("status", "")).lower() == "active"


def _norm_med_name(name: str) -> str:
    """Normalize a medication display name for cross-store matching.

    Lower-case and whitespace-collapsed only — deliberately conservative.
    Production would reconcile on RxNorm codes, not display text.
    """
    return " ".join(name.lower().split())


def _med_ref(rtype: ResourceType, rid: str, name: str) -> FhirReference:
    return FhirReference(
        resource_type=rtype,
        resource_id=rid,
        field="medicationCodeableConcept",
        value=name,
    )


def medication_reconciliation(context: Any) -> list[VerificationDomainFlag]:
    """Reconcile prescribed orders against the reported medication list.

    Compares ``MedicationRequest`` (orders/prescriptions) against
    ``MedicationStatement`` (reported/home list) and flags divergences the
    two stores could not agree on: a drug present in one store but not the
    other, or an active/inactive disagreement for the same drug.

    Reconciliation only runs when **both** stores are populated — with no
    reported list there is nothing to reconcile against, so a patient who has
    prescriptions but no documented home list produces no flags.
    """
    orders: dict[str, tuple[bool, str, str]] = {}
    statements: dict[str, tuple[bool, str, str]] = {}
    for (rtype, rid), res in context.resources_by_key.items():
        if rtype == ResourceType.MedicationRequest:
            name = _display_name(res)
            if name:
                orders[_norm_med_name(name)] = (_status_active(res), rid, name)
        elif rtype == ResourceType.MedicationStatement:
            name = _display_name(res)
            if name:
                statements[_norm_med_name(name)] = (_status_active(res), rid, name)

    if not orders or not statements:
        return []

    flags: list[VerificationDomainFlag] = []
    for key in sorted(set(orders) | set(statements)):
        in_orders = key in orders
        in_statements = key in statements
        if in_orders and not in_statements:
            _active, rid, name = orders[key]
            flags.append(
                VerificationDomainFlag(
                    rule="medication_reconciliation",
                    severity="warning",
                    message=(
                        f"'{name}' is prescribed (MedicationRequest) but absent "
                        f"from the reported medication list (MedicationStatement)."
                    ),
                    must_surface=True,
                    evidence=[_med_ref(ResourceType.MedicationRequest, rid, name)],
                )
            )
        elif in_statements and not in_orders:
            _active, rid, name = statements[key]
            flags.append(
                VerificationDomainFlag(
                    rule="medication_reconciliation",
                    severity="warning",
                    message=(
                        f"'{name}' is on the reported medication list "
                        f"(MedicationStatement) but has no matching order "
                        f"(MedicationRequest)."
                    ),
                    must_surface=True,
                    evidence=[_med_ref(ResourceType.MedicationStatement, rid, name)],
                )
            )
        else:
            order_active, order_id, order_name = orders[key]
            stmt_active, stmt_id, _stmt_name = statements[key]
            if order_active == stmt_active:
                continue
            order_state = "active" if order_active else "inactive"
            stmt_state = "active" if stmt_active else "inactive"
            flags.append(
                VerificationDomainFlag(
                    rule="medication_reconciliation",
                    severity="warning",
                    message=(
                        f"'{order_name}' status disagrees between stores: "
                        f"{order_state} order (MedicationRequest) vs {stmt_state} "
                        f"on the reported list (MedicationStatement)."
                    ),
                    must_surface=True,
                    evidence=[
                        _med_ref(ResourceType.MedicationRequest, order_id, order_name),
                        _med_ref(ResourceType.MedicationStatement, stmt_id, order_name),
                    ],
                )
            )
    return flags


def default_rules() -> tuple[DomainRule, ...]:
    """The MVP rule set — extendable but load-bearing today."""
    return (
        allergy_medication_conflict,
        critical_lab,
        reference_range,
        medication_reconciliation,
    )
