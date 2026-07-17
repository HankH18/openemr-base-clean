"""Deterministic, honest chat agent — no API key required.

``StubAgent`` fetches the patient's resources over FHIR, then emits a
``Claim`` for a resource only when the question actually refers to it
(token substring match against the resource's display string) or when the
question is a generic summary request.  If nothing matches, it says so and
emits zero claims — it never fabricates.

Every ``(field, value)`` pair it emits is extracted with the *same* function
the verification layer uses (via ``copilot.agent.grounding``), so a claim this
agent produces is guaranteed to survive the deterministic value-match gate.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, cast

from copilot.agent.base import (
    AgentAnswer,
    ConversationTurn,
    render_document_facts,
    render_guideline_evidence,
)
from copilot.agent.grounding import claim_text, describe_resource, extract_temporal, extract_unit
from copilot.domain.contracts import Claim
from copilot.domain.documents import ExtractedFact
from copilot.domain.primitives import FhirReference, PatientId, ResourceType
from copilot.fhir.client import FhirClient
from copilot.rag.retriever import GuidelineEvidence

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

# A message containing any of these is treated as "tell me everything".
_SUMMARY_KEYWORDS: tuple[str, ...] = ("summar", "overview", "everything", "all")

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_MIN_TOKEN_LEN = 3

_NO_MATCH_ANSWER = "I can't confirm that from this patient's record."

# Prefixes for the multi-agent graph's worker output. Both are appended to the
# grounded prose and never merged into it: a document fact and a guideline
# recommendation must stay legible as *not* a verified FHIR claim.
_DOCUMENT_PREFIX = " From the attached document(s): "
_GUIDELINE_PREFIX = " Guideline context (from the guideline corpus, not this patient's record): "


def _worker_context(
    guideline_evidence: list[GuidelineEvidence] | None,
    document_facts: list[ExtractedFact] | None,
) -> str:
    """The graph workers' output rendered as a prose suffix ("" when there is none).

    With no worker output — every inline (flag-OFF) call — this returns the empty
    string, so the answer is byte-for-byte what the stub produced before the
    graph could feed it anything.
    """
    parts: list[str] = []
    if document_facts:
        lines = render_document_facts(document_facts)
        if lines:
            parts.append(_DOCUMENT_PREFIX + "; ".join(lines) + ".")
    if guideline_evidence:
        lines = render_guideline_evidence(guideline_evidence)
        if lines:
            parts.append(_GUIDELINE_PREFIX + " ".join(lines))
    return "".join(parts)


class StubAgent:
    """A ``ChatAgent`` that grounds every claim or says nothing at all."""

    def __init__(self, fhir_client: FhirClient) -> None:
        self._fhir = fhir_client

    async def answer(
        self,
        patient_id: PatientId,
        message: str,
        history: list[ConversationTurn] | None = None,
        *,
        guideline_evidence: list[GuidelineEvidence] | None = None,
        document_facts: list[ExtractedFact] | None = None,
    ) -> AgentAnswer:
        resources = await self._fetch(patient_id)
        resources.sort(key=lambda r: str(r.get("id", "")))

        tokens = {t for t in _TOKEN_RE.findall(message.lower()) if len(t) >= _MIN_TOKEN_LEN}
        message_lower = message.lower()
        is_summary = any(kw in message_lower for kw in _SUMMARY_KEYWORDS)

        claims: list[Claim] = []
        for res in resources:
            described = describe_resource(res)
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
            unit = extract_unit(res)
            claims.append(
                Claim(
                    text=claim_text(rtype, display, value, unit),
                    source_ref=FhirReference(
                        resource_type=ResourceType(rtype),
                        resource_id=rid,
                        field=field,
                        value=value,
                        timestamp=extract_temporal(res),
                        unit=unit,
                    ),
                )
            )

        if not claims:
            return AgentAnswer(answer=_NO_MATCH_ANSWER, claims=[])

        prose = "Based on this patient's record: " + " ".join(c.text for c in claims)
        # Worker output (graph path only) extends the prose; the claims — the
        # audited evidence — stay exactly the FHIR-grounded set above.
        prose += _worker_context(guideline_evidence, document_facts)
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
