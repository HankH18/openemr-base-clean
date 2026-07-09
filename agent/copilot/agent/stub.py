"""Deterministic, honest chat agent — no API key required.

``StubAgent`` fetches the patient's resources over FHIR, then emits a
``Claim`` for a resource only when the question actually refers to it
(token substring match against the resource's display string) or when the
question is a generic summary request.  If nothing matches, it says so and
emits zero claims — it never fabricates.

Every ``(field, value)`` pair it emits is extracted with the *same*
function the verification layer uses (``extract_field_value``), so a claim
this agent produces is guaranteed to survive the deterministic value-match
gate.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, cast

from copilot.agent.base import AgentAnswer, ConversationTurn
from copilot.domain.contracts import Claim
from copilot.domain.primitives import FhirReference, PatientId, ResourceType
from copilot.fhir.client import FhirClient
from copilot.verification.core import extract_field_value

# The resource types the stub pulls for one patient.  Order here is only
# the fetch order; emission is re-sorted by resource id for determinism.
_SEARCH_TYPES: tuple[ResourceType, ...] = (
    ResourceType.Observation,
    ResourceType.MedicationRequest,
    ResourceType.Condition,
    ResourceType.AllergyIntolerance,
    ResourceType.Encounter,
    ResourceType.DiagnosticReport,
)

# Display + (field, value) source for the "code.text"-style resources.
_CODE_TEXT_TYPES: dict[str, str] = {
    "Condition": "code.text",
    "DiagnosticReport": "code.text",
    "AllergyIntolerance": "code.text",
    "MedicationRequest": "medicationCodeableConcept.text",
}

# A message containing any of these is treated as "tell me everything".
_SUMMARY_KEYWORDS: tuple[str, ...] = ("summar", "overview", "everything", "all")

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_MIN_TOKEN_LEN = 3

_NO_MATCH_ANSWER = "I can't confirm that from this patient's record."


class StubAgent:
    """A ``ChatAgent`` that grounds every claim or says nothing at all."""

    def __init__(self, fhir_client: FhirClient) -> None:
        self._fhir = fhir_client

    async def answer(
        self,
        patient_id: PatientId,
        message: str,
        history: list[ConversationTurn] | None = None,
    ) -> AgentAnswer:
        resources = await self._fetch(patient_id)
        resources.sort(key=lambda r: str(r.get("id", "")))

        tokens = {t for t in _TOKEN_RE.findall(message.lower()) if len(t) >= _MIN_TOKEN_LEN}
        message_lower = message.lower()
        is_summary = any(kw in message_lower for kw in _SUMMARY_KEYWORDS)

        claims: list[Claim] = []
        for res in resources:
            described = _describe(res)
            if described is None:
                continue
            display, field, value = described
            if not display.strip():
                continue
            matched = is_summary or any(tok in display.lower() for tok in tokens)
            if not matched:
                continue
            rid = res.get("id")
            rtype = res.get("resourceType")
            if not isinstance(rid, str) or not isinstance(rtype, str):
                continue
            claims.append(
                Claim(
                    text=_claim_text(rtype, display, value),
                    source_ref=FhirReference(
                        resource_type=ResourceType(rtype),
                        resource_id=rid,
                        field=field,
                        value=value,
                    ),
                )
            )

        if not claims:
            return AgentAnswer(answer=_NO_MATCH_ANSWER, claims=[])

        prose = "Based on this patient's record: " + " ".join(c.text for c in claims)
        return AgentAnswer(answer=prose, claims=claims)

    async def _fetch(self, patient_id: PatientId) -> list[dict[str, Any]]:
        """Pull every configured resource type; skip a type on any error."""
        resources: list[dict[str, Any]] = []
        params = {"patient": str(patient_id)}
        for rtype in _SEARCH_TYPES:
            try:
                bundle = await self._fhir.search(rtype, params)
            except Exception:
                # A single failing resource type must not sink the whole
                # answer — the record we can read is still worth grounding on.
                continue
            entries = bundle.get("entry")
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, Mapping):
                    continue
                res = entry.get("resource")
                if isinstance(res, dict) and res.get("id") is not None:
                    resources.append(cast("dict[str, Any]", res))
        return resources


def _describe(resource: Mapping[str, Any]) -> tuple[str, str, str] | None:
    """Return ``(display, field, value)`` for a resource, or None to skip.

    ``display`` drives question-scoped matching; ``(field, value)`` is the
    verbatim source pointer for the emitted claim.  ``value`` is read with
    the verification layer's own extractor, so it is guaranteed to match a
    live re-fetch.
    """
    rtype = resource.get("resourceType")
    if not isinstance(rtype, str):
        return None

    if rtype == "Observation":
        display = extract_field_value(resource, "code.text")
        value = extract_field_value(resource, "valueQuantity.value")
        if display is None or value is None:
            return None
        return (display, "valueQuantity.value", value)

    if rtype in _CODE_TEXT_TYPES:
        field = _CODE_TEXT_TYPES[rtype]
        display = extract_field_value(resource, field)
        if display is None:
            return None
        return (display, field, display)

    if rtype == "Encounter":
        # Encounter has no code.text; fall back to the first sensible surface
        # field so a summary request can still cite it verbatim.
        for field in ("type[0].text", "class.code", "status"):
            value = extract_field_value(resource, field)
            if value is not None:
                return (value, field, value)

    return None


def _claim_text(resource_type: str, display: str, value: str) -> str:
    """A short factual sentence naming the resource and its value.

    Any numeric literal here is copied from ``display``/``value``, both of
    which came verbatim from the resource — so the verification numeric
    check always finds them in the source.
    """
    if resource_type == "Observation":
        return f"{resource_type} {display}: {value}."
    return f"{resource_type}: {display}."
