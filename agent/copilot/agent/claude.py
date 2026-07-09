"""Real Claude chat agent — Anthropic tool-use loop over the FHIR client.

``ClaudeAgent`` runs an agentic loop: Claude may call ``get_labs`` /
``get_medications`` (which read the patient's resources through the
injected ``FhirClient``), then must return a JSON object whose every claim
cites a ``source_ref``.  The parsed claims flow into the same verification
gate as everything else, so a fabricated citation is dropped downstream.

It refuses to construct without an API key (like ``ClaudeSynthesizer``).
It is *not* exercised in the keyless test path — the deterministic
``StubAgent`` carries correctness there — but it must import cleanly,
type-check, and construct with a key.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel

from copilot.agent.base import AgentAnswer, ConversationTurn
from copilot.config import Settings
from copilot.domain.contracts import Claim
from copilot.domain.primitives import FhirReference, PatientId, ResourceType
from copilot.fhir.client import FhirClient

_MAX_TOOL_ITERATIONS = 6
_MAX_TOKENS = 2048

_SYSTEM_PROMPT = """You are a clinical chat assistant for a hospitalist, \
answering questions about a single patient.

You have tools to read the patient's labs and medications from the record. \
Use them before answering any question that depends on clinical data.

Return your final message as a single JSON object with EXACTLY this shape \
(no prose outside the JSON, no code fences):

{
  "answer": "<a direct, factual answer to the question>",
  "claims": [
    {
      "text": "<one factual sentence naming the resource and its value>",
      "resource_type": "<FHIR resource type, e.g. Observation>",
      "resource_id": "<id of that resource>",
      "field": "<FHIRPath-like field the value came from>",
      "value": "<the value as a string, verbatim from the record>"
    }
  ]
}

Rules:
- Every claim MUST cite a resource returned by a tool call.
- Every number, dose, and medication name in a claim MUST match the record verbatim.
- If the record does not support the question, say so plainly and return "claims": [].
- Never invent a resource, value, or citation."""

_TOOLS: list[dict[str, Any]] = [
    {
        "name": "get_labs",
        "description": "Fetch the patient's Observation (lab/vital) resources as FHIR JSON.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_medications",
        "description": "Fetch the patient's MedicationRequest resources as FHIR JSON.",
        "input_schema": {"type": "object", "properties": {}},
    },
]

_TOOL_RESOURCE_TYPES: dict[str, ResourceType] = {
    "get_labs": ResourceType.Observation,
    "get_medications": ResourceType.MedicationRequest,
}


class AgentError(Exception):
    """Raised when the model output cannot be parsed into an AgentAnswer."""


class _ClaudeClaim(BaseModel):
    """Narrow wire shape Claude emits per claim."""

    text: str
    resource_type: str
    resource_id: str
    field: str
    value: str


class _ClaudeAnswer(BaseModel):
    """Full JSON shape Claude must return."""

    answer: str
    claims: list[_ClaudeClaim]


class ClaudeAgent:
    """Anthropic-backed ``ChatAgent`` — refuses to run without an API key."""

    def __init__(
        self,
        settings: Settings,
        fhir_client: FhirClient,
        client: object | None = None,
    ) -> None:
        if not settings.anthropic_api_key:
            raise AgentError("ANTHROPIC_API_KEY not set — ClaudeAgent refuses to run.")
        self._fhir = fhir_client
        self._model = settings.anthropic_model_synthesis
        if client is not None:
            self._client: Any = client
        else:
            from anthropic import AsyncAnthropic  # local import keeps the stub path light

            self._client = AsyncAnthropic(api_key=settings.anthropic_api_key)

    async def answer(
        self,
        patient_id: PatientId,
        message: str,
        history: list[ConversationTurn] | None = None,
    ) -> AgentAnswer:
        messages: list[dict[str, Any]] = [
            {"role": turn.role, "content": turn.content} for turn in (history or [])
        ]
        messages.append({"role": "user", "content": message})

        response: Any = None
        for _ in range(_MAX_TOOL_ITERATIONS):
            response = await self._client.messages.create(
                model=self._model,
                max_tokens=_MAX_TOKENS,
                system=_SYSTEM_PROMPT,
                tools=_TOOLS,
                messages=messages,
            )
            if getattr(response, "stop_reason", None) != "tool_use":
                break
            messages.append({"role": "assistant", "content": response.content})
            messages.append(
                {"role": "user", "content": await self._run_tools(response, patient_id)}
            )

        return self._parse(_extract_text(response))

    async def _run_tools(self, response: Any, patient_id: PatientId) -> list[dict[str, Any]]:
        """Execute every tool_use block and return the tool_result blocks."""
        results: list[dict[str, Any]] = []
        for block in getattr(response, "content", []) or []:
            if getattr(block, "type", None) != "tool_use":
                continue
            resource_type = _TOOL_RESOURCE_TYPES.get(getattr(block, "name", ""))
            if resource_type is None:
                content = "Unknown tool."
            else:
                try:
                    bundle = await self._fhir.search(resource_type, {"patient": str(patient_id)})
                    content = json.dumps(bundle, ensure_ascii=False)
                except Exception:
                    content = f"Failed to fetch {resource_type.value} resources."
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": content})
        return results

    def _parse(self, text: str) -> AgentAnswer:
        try:
            payload = _ClaudeAnswer.model_validate_json(text)
        except Exception as exc:
            raise AgentError(f"Claude output was not valid JSON: {exc}") from exc

        claims: list[Claim] = []
        for c in payload.claims:
            try:
                rtype = ResourceType(c.resource_type)
            except ValueError as exc:
                raise AgentError(f"unknown FHIR resource type: {c.resource_type}") from exc
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
        return AgentAnswer(answer=payload.answer, claims=claims)


def _extract_text(response: Any) -> str:
    """Join the text parts of an Anthropic response's content blocks."""
    parts: list[str] = []
    for block in getattr(response, "content", []) or []:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts)
