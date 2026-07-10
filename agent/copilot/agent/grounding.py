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


def claim_text(resource_type: str, display: str, value: str) -> str:
    """A short factual sentence naming the resource and its value.

    The type prefix and concept label are humanized so emitted text reads
    cleanly ("Medication: Hydromorphone." not "MedicationRequest: Hydromorphone.";
    "Observation Oxygen Saturation: 98." not "... oxygen_saturation: 98."). The
    numeric ``value`` is kept verbatim — it comes straight from the resource, so
    the verification numeric check always finds it in source.
    """
    type_label = humanize_label(resource_type)
    concept = humanize_label(display)
    if resource_type == "Observation":
        return f"{type_label} {concept}: {value}."
    return f"{type_label}: {concept}."
