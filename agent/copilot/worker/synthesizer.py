"""LLM synthesizer — pulls a patient's changed resources into a memory file.

Two implementations:

- ``ClaudeSynthesizer`` calls Anthropic's Messages API with a strict JSON
  instruction and parses the output into ``MemoryFileSummary``.
- ``StubSynthesizer`` is a deterministic fake used in tests: it produces
  one claim per input resource with a source_ref pointing at its ID.

The protocol is small on purpose — verification is where all the
correctness lives; this class only turns resources into a *proposed*
summary.  Verification is run at synthesis time (Poller) so anything the
LLM fabricates never lands in the store.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from pydantic import BaseModel

from copilot.domain.contracts import Claim, MemoryFileSummary
from copilot.domain.primitives import FhirReference, PatientId, ResourceType, utcnow


class SynthesisError(Exception):
    """Raised when the model output cannot be parsed into a MemoryFileSummary."""


@dataclass(frozen=True)
class SynthesisInput:
    """What the synthesizer sees.

    A bundle of raw FHIR resource dicts (from ``fhir-client``) plus the
    watermark that will be recorded if synthesis passes verification.
    """

    patient_id: PatientId
    resources: Sequence[Mapping[str, Any]]
    source_watermark: datetime


class LlmSynthesizer(Protocol):
    async def synthesize(self, inputs: SynthesisInput) -> MemoryFileSummary:
        """Return a proposed memory-file summary; caller runs verification."""


class _ClaudeClaim(BaseModel):
    """Wire shape we ask Claude to emit — narrower than the domain Claim."""

    text: str
    resource_type: str
    resource_id: str
    field: str
    value: str


class _ClaudeSynthesizerResponse(BaseModel):
    """Full JSON shape Claude must return."""

    claims: list[_ClaudeClaim]
    acuity_score: float
    rank_reason: str


class StubSynthesizer:
    """Fully deterministic — safe for unit tests, no API keys required.

    Emits one Claim per input resource, pulling a plausible `field`/`value`
    from the resource shape (Observation.valueQuantity.value → value).
    """

    async def synthesize(self, inputs: SynthesisInput) -> MemoryFileSummary:
        claims: list[Claim] = []
        for res in inputs.resources:
            rtype = res.get("resourceType")
            rid = res.get("id")
            if rtype is None or rid is None:
                continue
            field, value = _extract_stub_field(res)
            claims.append(
                Claim(
                    text=f"{rtype}/{rid} → {field}={value}",
                    source_ref=FhirReference(
                        resource_type=ResourceType(rtype)
                        if rtype in ResourceType.__members__
                        else ResourceType.Observation,
                        resource_id=str(rid),
                        field=field,
                        value=value,
                    ),
                )
            )
        from copilot.worker.hashing import content_hash_for_resources

        return MemoryFileSummary(
            patient_id=inputs.patient_id,
            claims=claims,
            acuity_score=0.0,
            rank_reason="stub",
            synthesized_at=utcnow(),
            source_watermark=inputs.source_watermark,
            content_hash=content_hash_for_resources(list(inputs.resources)),
        )


def _extract_stub_field(res: Mapping[str, Any]) -> tuple[str, str]:
    """Best-effort (field, value) extraction for the stub."""
    if "valueQuantity" in res and isinstance(res["valueQuantity"], Mapping):
        v = res["valueQuantity"].get("value")
        return "valueQuantity.value", str(v) if v is not None else ""
    if "valueString" in res:
        return "valueString", str(res["valueString"])
    if "status" in res:
        return "status", str(res["status"])
    return "id", str(res.get("id", ""))


# --- Claude ----------------------------------------------------------------

_SYNTHESIS_SYSTEM_PROMPT = """You are a clinical summarizer for a hospitalist.

For the patient's changed FHIR resources you receive, produce a JSON
object with EXACTLY this shape (no prose outside the JSON):

{
  "claims": [
    {
      "text": "<one sentence, factual, no interpretation the record does not directly support>",
      "resource_type": "<FHIR resource type, e.g. Observation>",
      "resource_id": "<id of that resource>",
      "field": "<FHIRPath-like field the claim is extracted from>",
      "value": "<the extracted value as a string, verbatim from source>"
    }
  ],
  "acuity_score": <float 0.0–10.0>,
  "rank_reason": "<one short sentence explaining acuity_score>"
}

Rules:
- Every claim MUST cite a resource that appears in the input.
- Every number, dose, and med name in `text` MUST match the record verbatim.
- Do NOT invent claims. If unsure, omit.
- Output MUST be a single JSON object — no code fences, no commentary.
"""


class ClaudeSynthesizer:
    """Real Claude wrapper — refuses to run without ANTHROPIC_API_KEY."""

    def __init__(
        self,
        anthropic_api_key: str,
        model: str,
        client: object | None = None,
    ) -> None:
        if not anthropic_api_key:
            raise SynthesisError(
                "ANTHROPIC_API_KEY not set — ClaudeSynthesizer refuses to run."
            )
        self._model = model
        if client is not None:
            self._client = client
        else:
            from anthropic import AsyncAnthropic  # local import: keeps test path lightweight

            self._client = AsyncAnthropic(api_key=anthropic_api_key)

    async def synthesize(self, inputs: SynthesisInput) -> MemoryFileSummary:
        user_content = json.dumps(
            {
                "patient_id": inputs.patient_id.value,
                "resources": list(inputs.resources),
            },
            ensure_ascii=False,
        )
        # Kept off the strict typing path so we can swap in a fake client.
        response = await self._client.messages.create(  # type: ignore[attr-defined]
            model=self._model,
            max_tokens=2048,
            system=_SYNTHESIS_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}],
        )
        text = _extract_text(response)
        try:
            payload = _ClaudeSynthesizerResponse.model_validate_json(text)
        except Exception as exc:  # noqa: BLE001
            raise SynthesisError(f"Claude output was not valid JSON: {exc}") from exc

        claims: list[Claim] = []
        for c in payload.claims:
            try:
                rtype = ResourceType(c.resource_type)
            except ValueError as exc:
                raise SynthesisError(f"unknown FHIR resource type: {c.resource_type}") from exc
            claims.append(
                Claim(
                    text=c.text,
                    source_ref=FhirReference(
                        resource_type=rtype,
                        resource_id=c.resource_id,
                        field=c.field,
                        value=c.value,
                    ),
                )
            )
        from copilot.worker.hashing import content_hash_for_resources

        return MemoryFileSummary(
            patient_id=inputs.patient_id,
            claims=claims,
            acuity_score=payload.acuity_score,
            rank_reason=payload.rank_reason,
            synthesized_at=utcnow(),
            source_watermark=inputs.source_watermark,
            content_hash=content_hash_for_resources(list(inputs.resources)),
        )


def _extract_text(response: Any) -> str:
    """Anthropic SDK: response.content is a list of ContentBlock; join text parts."""
    parts: list[str] = []
    for block in getattr(response, "content", []) or []:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts)
