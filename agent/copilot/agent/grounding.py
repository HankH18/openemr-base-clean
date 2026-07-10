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


def claim_text(resource_type: str, display: str, value: str) -> str:
    """A short factual sentence naming the resource and its value.

    Numeric literals come verbatim from ``display``/``value`` (both read from
    the resource), so the verification numeric check always finds them in source.
    """
    if resource_type == "Observation":
        return f"{resource_type} {display}: {value}."
    return f"{resource_type}: {display}."
