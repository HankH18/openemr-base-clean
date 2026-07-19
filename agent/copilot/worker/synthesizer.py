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
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict

from copilot.domain.contracts import Claim, MemoryFileSummary
from copilot.domain.primitives import FhirReference, PatientId, ResourceType, utcnow
from copilot.observability.pricing import cost_usd
from copilot.resilience import SYNTHESIS_MAX_RETRIES, SYNTHESIS_TIMEOUT

_logger = logging.getLogger(__name__)


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

    # Untrusted LLM output: a ValidationError here is stringified into
    # SynthesisError and emitted in the poller.result observability event.
    # Strip the parsed value (synthesized clinical claims) from the error text
    # while keeping the field path + error type — matching the extraction
    # schemas hardened in edf8b24 (see copilot.domain.documents).
    model_config = ConfigDict(hide_input_in_errors=True)

    claims: list[_ClaudeClaim]
    acuity_score: float
    rank_reason: str


class StubSynthesizer:
    """Fully deterministic — safe for unit tests, no API keys required.

    Emits one Claim per input resource, pulling a plausible `field`/`value`
    from the resource shape (Observation.valueQuantity.value → value).
    """

    async def synthesize(self, inputs: SynthesisInput) -> MemoryFileSummary:
        # Human-readable claim text via the same grounding the chat agents use,
        # so a card reads "Observation Potassium: 5.7 mmol/L", not
        # "Observation/<uuid> → valueQuantity.value=5.7". Falls back to the raw
        # pointer only when a resource has no groundable concept/value.
        from copilot.agent.grounding import claim_text, describe_resource, extract_unit

        claims: list[Claim] = []
        for res in inputs.resources:
            rtype = res.get("resourceType")
            rid = res.get("id")
            if rtype is None or rid is None:
                continue
            described = describe_resource(res)
            if described is None:
                # No groundable concept/value (e.g. a vital-signs panel container
                # whose only field is status=final) — skip it rather than surface
                # a clinically meaningless "Observation/<uuid>" line on the card.
                continue
            display, field, value = described
            # Same (text, source_ref) pairing the chat agents use: one verbatim
            # unit feeds BOTH, so the card renders a quantity and the gate has a
            # unit to re-compare. Grounded via extract_unit — the extractor the
            # verifier itself re-runs — so it agrees byte-for-byte on re-fetch.
            unit = extract_unit(res)
            claims.append(
                Claim(
                    text=claim_text(str(rtype), display, str(value), unit),
                    source_ref=FhirReference(
                        resource_type=ResourceType(rtype)
                        if rtype in ResourceType.__members__
                        else ResourceType.Observation,
                        resource_id=str(rid),
                        field=field,
                        value=str(value),
                        unit=unit,
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
  "acuity_score": <float 0.0-10.0>,
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
            raise SynthesisError("ANTHROPIC_API_KEY not set — ClaudeSynthesizer refuses to run.")
        self._model = model
        if client is not None:
            self._client = client
        else:
            from anthropic import AsyncAnthropic  # local import: keeps test path lightweight

            # Explicit, not inherited — the SDK default read timeout is 600s.
            # Background synthesis blocks no clinician, so this is the loosest
            # non-vision budget; see copilot.resilience.
            self._client = AsyncAnthropic(
                api_key=anthropic_api_key,
                timeout=SYNTHESIS_TIMEOUT,
                max_retries=SYNTHESIS_MAX_RETRIES,
            )

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
        self._log_usage(response, inputs.patient_id)
        text = _extract_text(response)
        try:
            payload = _ClaudeSynthesizerResponse.model_validate_json(text)
        except Exception as exc:
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

    def _log_usage(self, response: Any, patient_id: PatientId) -> None:
        """Record synthesis token usage + computed USD cost to the logs.

        The synthesizer has no observability handle (it runs deep inside the
        poller tick), so its spend is surfaced as a structured log line rather
        than a span — enough to answer "how much did background synthesis cost".
        A response with no ``usage`` contributes zero and logs nothing.
        """
        input_tokens = _usage_tokens(response, "input_tokens")
        output_tokens = _usage_tokens(response, "output_tokens")
        if input_tokens == 0 and output_tokens == 0:
            return
        _logger.info(
            "synthesis LLM usage",
            extra={
                "model": self._model,
                "patient_id": patient_id.value,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cost_usd": cost_usd(self._model, input_tokens, output_tokens),
            },
        )


def _usage_tokens(response: Any, field: str) -> int:
    """Read one integer counter off an Anthropic response's ``usage``.

    Defensive: a response with no ``usage`` (or a non-int counter) contributes
    zero, so a fake client or a partial response never breaks the tally.
    """
    usage = getattr(response, "usage", None)
    value = getattr(usage, field, None)
    return value if isinstance(value, int) else 0


def _extract_text(response: Any) -> str:
    """Anthropic SDK: response.content is a list of ContentBlock; join text parts."""
    parts: list[str] = []
    for block in getattr(response, "content", []) or []:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts)
