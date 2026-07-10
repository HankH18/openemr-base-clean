"""Real Claude chat agent — Anthropic tool-use loop over the FHIR client.

``ClaudeAgent`` runs an agentic loop: Claude may call ``get_labs`` /
``get_medications`` (which read the patient's resources through the injected
``FhirClient``), then returns JSON naming, per clinical claim, the *resource it
came from*. The code — not the model — then builds each ``source_ref`` from the
actual fetched resource (via ``copilot.agent.grounding``), so every emitted claim
is grounded in real data and survives the deterministic value-match gate. Claude's
prose ``answer`` is kept as the narrative; the claims are the audited evidence.

Refuses to construct without an API key. Not exercised in the keyless test path
(``StubAgent`` carries correctness there), but must import + type-check cleanly.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel

from copilot.agent.base import AgentAnswer, ConversationTurn
from copilot.agent.grounding import claim_text, describe_resource
from copilot.config import Settings
from copilot.domain.contracts import Claim
from copilot.domain.primitives import FhirReference, PatientId, ResourceType
from copilot.fhir.client import FhirClient

_MAX_TOOL_ITERATIONS = 6
_MAX_TOKENS = 2048

_SYSTEM_PROMPT = """You are a clinical chat assistant for a hospitalist, \
answering questions about a single patient.

You have tools to read the patient's labs and medications from the record. Use \
them before answering any question that depends on clinical data.

Return your final message as a single JSON object with EXACTLY this shape (no \
prose outside the JSON, no code fences):

{
  "answer": "<a direct, factual clinical answer to the question>",
  "claims": [
    {
      "resource_type": "<FHIR resource type of a resource a tool returned, e.g. Observation>",
      "resource_id": "<the id of that exact resource>"
    }
  ]
}

Rules:
- List one claim per resource that backs your answer; cite ONLY resources a tool
  actually returned, by their exact id. Do not invent ids.
- The system fills in the precise field/value from the cited resource — you only
  name which resource supports each point, and write the prose answer.
- If the record does not support the question, say so plainly in "answer" and
  return "claims": []."""

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
    """Narrow wire shape Claude emits per claim — just a pointer to a resource."""

    resource_type: str
    resource_id: str


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

        # Cache every resource the tools return, keyed by (resourceType, id), so
        # claims can be grounded against the exact fetched record.
        fetched: dict[tuple[str, str], dict[str, Any]] = {}

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
                {"role": "user", "content": await self._run_tools(response, patient_id, fetched)}
            )

        return self._build_answer(_extract_text(response), fetched)

    async def _run_tools(
        self, response: Any, patient_id: PatientId, fetched: dict[tuple[str, str], dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Execute every tool_use block, cache the resources, return tool_results."""
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
                    _cache_bundle(bundle, fetched)
                    content = json.dumps(bundle, ensure_ascii=False)
                except Exception:
                    content = f"Failed to fetch {resource_type.value} resources."
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": content})
        return results

    def _build_answer(
        self, text: str, fetched: dict[tuple[str, str], dict[str, Any]]
    ) -> AgentAnswer:
        payload = _parse_answer(text)
        if payload is None:
            # The model didn't return parseable JSON — markdown fences, or a prose
            # refusal to an out-of-scope question. Fail closed: no claims means the
            # chat service withholds with an honest message. A chat turn must never
            # 500 on a model formatting quirk.
            return AgentAnswer(answer="", claims=[])

        claims: list[Claim] = []
        for c in payload.claims:
            resource = fetched.get((c.resource_type, c.resource_id))
            if resource is None:
                continue  # cited a resource no tool returned — drop it (fail closed)
            described = describe_resource(resource)
            if described is None:
                continue
            display, field, value = described
            try:
                rtype = ResourceType(c.resource_type)
            except ValueError:
                continue
            claims.append(
                Claim(
                    text=claim_text(c.resource_type, display, value),
                    source_ref=FhirReference(
                        resource_type=rtype, resource_id=c.resource_id, field=field, value=value
                    ),
                )
            )
        return AgentAnswer(answer=payload.answer, claims=claims)


def _cache_bundle(bundle: dict[str, Any], fetched: dict[tuple[str, str], dict[str, Any]]) -> None:
    """Index a search Bundle's resources by (resourceType, id)."""
    entries = bundle.get("entry")
    if not isinstance(entries, list):
        return
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        res = entry.get("resource")
        if not isinstance(res, dict):
            continue
        rtype = res.get("resourceType")
        rid = res.get("id")
        if isinstance(rtype, str) and isinstance(rid, str):
            fetched[(rtype, rid)] = res


def _parse_answer(text: str) -> _ClaudeAnswer | None:
    """Parse the model reply into ``_ClaudeAnswer``, tolerating fences/prose.

    Returns None when no valid answer object can be recovered, so the caller can
    fail closed rather than raise — a chat turn must not 500 on a model quirk.
    """
    candidate = _json_object_slice(text)
    if candidate is None:
        return None
    try:
        return _ClaudeAnswer.model_validate_json(candidate)
    except Exception:
        return None


def _json_object_slice(text: str) -> str | None:
    """The outermost ``{...}`` span in ``text`` — strips code fences / prose."""
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    return text[start : end + 1]


def _extract_text(response: Any) -> str:
    """Join the text parts of an Anthropic response's content blocks."""
    parts: list[str] = []
    for block in getattr(response, "content", []) or []:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts)
