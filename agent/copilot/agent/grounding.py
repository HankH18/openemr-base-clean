"""Shared FHIR-resource grounding used by both chat agents.

Both ``StubAgent`` and ``ClaudeAgent`` build every claim's ``source_ref`` from
the *actual fetched resource* via the verification layer's own extractor
(``extract_field_value``) — so a claim is guaranteed to survive the deterministic
value-match gate. The LLM decides which resources are relevant; the code fills the
exact (field, value) pair. This keeps the trust boundary on deterministic code, not
on the model transcribing values correctly.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from copilot.verification.core import extract_field_value

# The CodeableConcept root each resource type names its clinical concept with.
_CONCEPT_ROOT: dict[str, str] = {
    "Condition": "code",
    "DiagnosticReport": "code",
    "AllergyIntolerance": "code",
    "MedicationRequest": "medicationCodeableConcept",
}


def _concept_display(resource: Mapping[str, Any], root: str) -> tuple[str, str] | None:
    """Best ``(field, value)`` label for a CodeableConcept.

    OpenEMR's FHIR often leaves ``.text`` empty and carries the label in
    ``coding[0].display`` (or just the ``.code``); the acceptance fake uses
    ``.text``. Try each so both real and fake data ground.
    """
    for path in (f"{root}.text", f"{root}.coding[0].display", f"{root}.coding[0].code"):
        value = extract_field_value(resource, path)
        if value is not None and str(value).strip():
            return (path, value)
    return None


def describe_resource(resource: Mapping[str, Any]) -> tuple[str, str, str] | None:
    """Return ``(display, field, value)`` for a resource, or None to skip.

    ``display`` is a human label (drives question matching); ``(field, value)``
    is the verbatim source pointer, read with the verification extractor so it
    matches a live re-fetch exactly.
    """
    rtype = resource.get("resourceType")
    if not isinstance(rtype, str):
        return None

    if rtype == "Observation":
        # The numeric value is the gate-critical fact; the code label is only
        # for display, so a missing label must not drop a real lab result.
        value = extract_field_value(resource, "valueQuantity.value")
        if value is None:
            return None
        concept = _concept_display(resource, "code")
        display = concept[1] if concept else "Observation"
        return (display, "valueQuantity.value", value)

    root = _CONCEPT_ROOT.get(rtype)
    if root is not None:
        concept = _concept_display(resource, root)
        if concept is None:
            return None
        field, value = concept
        return (value, field, value)

    if rtype == "Encounter":
        for field in ("type[0].text", "type[0].coding[0].display", "class.code", "status"):
            value = extract_field_value(resource, field)
            if value is not None:
                return (value, field, value)

    return None


def extract_unit(resource: Mapping[str, Any]) -> str | None:
    """The unit of a resource's quantity value, as a raw verbatim string.

    ``valueQuantity.unit`` for an ``Observation``; ``None`` for every other
    resource type (a medication name and a date have no dimension). Read through
    the verification layer's own ``extract_field_value`` — the *same* extractor
    the gate uses — so a unit grounded here agrees byte-for-byte with what a live
    re-fetch re-derives.

    Returns ``None`` when the field is absent, so a unit-less claim is left
    untouched by the unit gate (the same short-circuit ``extract_temporal`` gets:
    nothing grounded ⇒ nothing to re-verify).

    Grounded as a sibling of the value rather than folded into it because
    ``FhirReference.value`` must keep matching ``extract_field_value(resource,
    ref.field)`` verbatim — appending a unit to it would break the value gate it
    is meant to strengthen.
    """
    if resource.get("resourceType") == "Observation":
        return extract_field_value(resource, "valueQuantity.unit")
    return None


def extract_temporal(resource: Mapping[str, Any]) -> str | None:
    """The clinically meaningful timestamp of a resource, as a raw ISO string.

    ``authoredOn`` for a ``MedicationRequest``; ``effectiveDateTime`` (then
    ``issued``) for an ``Observation``. Read through the verification layer's own
    ``extract_field_value`` — the *same* extractor the gate uses — so a value
    grounded here agrees byte-for-byte with what a live re-fetch re-derives.

    Returns ``None`` for any other resource type, or when the field is absent, so
    a timestamp-less claim is left entirely untouched by the temporal gate (the
    fail-closed short-circuit: no timestamp grounded ⇒ nothing to re-verify).
    """
    rtype = resource.get("resourceType")
    if rtype == "MedicationRequest":
        return extract_field_value(resource, "authoredOn")
    if rtype == "Observation":
        for path in ("effectiveDateTime", "issued"):
            value = extract_field_value(resource, path)
            if value is not None:
                return value
    return None


# A raw FHIR resource type maps straight to its doctor-facing noun; this wins
# before any casing heuristic. Mirrors ``RESOURCE_LABELS`` in web/src/labels.ts.
_RESOURCE_LABELS: dict[str, str] = {
    "MedicationRequest": "Medication",
    "Condition": "Condition",
    "AllergyIntolerance": "Allergy",
    "Observation": "Observation",
    "Immunization": "Immunization",
    "Procedure": "Procedure",
    "DiagnosticReport": "Diagnostic report",
}

def humanize_label(label: str) -> str:
    """Normalize a raw FHIR type or code display to a doctor-facing label.

    Mirrors the frontend's ``humanizeLabel`` (``web/src/labels.ts``) so a claim's
    raw text already matches what the UI would render — re-humanizing on display
    is then a no-op. The resource-type map wins first; otherwise snake_case is
    split into words and each ordinary lowercase word is title-cased. A word that
    already carries an internal uppercase letter is left untouched, so acronyms
    and mixed-case abbreviations (``WBC``, ``BUN``, ``aPTT``) survive verbatim
    rather than being split or lower-cased.

    Examples: ``"oxygen_saturation"`` -> ``"Oxygen Saturation"``,
    ``"Heart rate"`` -> ``"Heart Rate"``, ``"MedicationRequest"`` ->
    ``"Medication"``, ``"WBC"`` -> ``"WBC"``, ``"aPTT"`` -> ``"aPTT"``.
    """
    mapped = _RESOURCE_LABELS.get(label.strip())
    if mapped is not None:
        return mapped

    def _cap(word: str) -> str:
        # An internal uppercase letter marks an acronym / mixed-case abbreviation
        # (WBC, BUN, aPTT) — leave it verbatim; otherwise title-case the word.
        if any(ch.isupper() for ch in word[1:]):
            return word
        return word[0].upper() + word[1:]

    # ``str.split()`` (no arg) collapses whitespace runs and drops empty tokens.
    return " ".join(_cap(word) for word in label.strip().replace("_", " ").split())


def claim_text(resource_type: str, display: str, value: str, unit: str | None = None) -> str:
    """A short factual sentence naming the resource and its value.

    The type prefix and concept label are humanized so emitted text reads
    cleanly ("Medication: Hydromorphone." not "MedicationRequest: Hydromorphone.";
    "Observation Oxygen Saturation: 98." not "... oxygen_saturation: 98."). The
    numeric ``value`` is kept verbatim — it comes straight from the resource, so
    the verification numeric check always finds it in source.

    ``unit`` (from :func:`extract_unit`) renders the reading as a QUANTITY —
    "Observation Troponin I: 2.34 ng/mL." rather than a dimensionless
    "Observation Troponin I: 2.34." A clinician cannot judge a lab from a bare
    number, and the number is the part the gate verifies.

    It is optional and defaults to ``None`` because this same function writes
    non-quantity claims (a medication name, a condition) and unit-less
    Observations: an absent or blank unit renders the value alone, never the
    string "None". Like ``value``, the unit is emitted VERBATIM and never passed
    through :func:`humanize_label` — that would title-case "mg" into "Mg",
    turning milligrams into megagrams.
    """
    type_label = humanize_label(resource_type)
    concept = humanize_label(display)
    if resource_type == "Observation":
        quantity = f"{value} {unit.strip()}" if unit and unit.strip() else value
        return f"{type_label} {concept}: {quantity}."
    return f"{type_label}: {concept}."
