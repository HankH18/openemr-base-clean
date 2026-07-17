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

from copilot.agent.base import (
    AgentAnswer,
    ConversationTurn,
    render_document_facts,
    render_guideline_evidence,
)
from copilot.agent.grounding import claim_text, describe_resource, extract_temporal
from copilot.config import Settings
from copilot.domain.contracts import Claim
from copilot.domain.documents import ExtractedFact
from copilot.domain.primitives import FhirReference, PatientId, ResourceType
from copilot.fhir.client import FhirClient
from copilot.rag.retriever import GuidelineEvidence
from copilot.resilience import CHAT_MAX_RETRIES, CHAT_TIMEOUT

_MAX_TOOL_ITERATIONS = 6
_MAX_TOKENS = 2048

_SYSTEM_PROMPT = """You are a clinical chat assistant for a hospitalist, \
answering questions about a single patient.

You have tools to read the patient's labs and medications from the record. Use \
them before answering any question that depends on clinical data.

Tool results include each resource's clinical time (`MedicationRequest.authoredOn`, \
`Observation.effectiveDateTime`), which you may use to answer time-relative questions; \
the system still fills each claim's `source_ref` — including that timestamp — from the \
cited resource, so grounding holds.

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

            # CHAT_TIMEOUT / CHAT_MAX_RETRIES are passed EXPLICITLY rather than
            # inherited: the SDK's default read timeout is 600s, which let one
            # hung call hold a clinician's turn for ten minutes. See
            # copilot.resilience for the SLO each number is anchored to.
            self._client = AsyncAnthropic(
                api_key=settings.anthropic_api_key,
                timeout=CHAT_TIMEOUT,
                max_retries=CHAT_MAX_RETRIES,
            )

    async def answer(
        self,
        patient_id: PatientId,
        message: str,
        history: list[ConversationTurn] | None = None,
        *,
        guideline_evidence: list[GuidelineEvidence] | None = None,
        document_facts: list[ExtractedFact] | None = None,
    ) -> AgentAnswer:
        messages: list[dict[str, Any]] = [
            {"role": turn.role, "content": turn.content} for turn in (history or [])
        ]
        messages.append(
            {"role": "user", "content": _with_worker_context(message, guideline_evidence, document_facts)}
        )

        # Cache every resource the tools return, keyed by (resourceType, id), so
        # claims can be grounded against the exact fetched record.
        fetched: dict[tuple[str, str], dict[str, Any]] = {}

        # Accumulate usage across every model turn in the tool-use loop, so the
        # cost reported downstream is the whole turn's spend, not just the last
        # call. ``tool_calls`` counts the tool invocations the model made.
        input_tokens = 0
        output_tokens = 0
        tool_calls = 0

        response: Any = None
        for _ in range(_MAX_TOOL_ITERATIONS):
            response = await self._client.messages.create(
                model=self._model,
                max_tokens=_MAX_TOKENS,
                system=_SYSTEM_PROMPT,
                tools=_TOOLS,
                messages=messages,
            )
            input_tokens += _usage_tokens(response, "input_tokens")
            output_tokens += _usage_tokens(response, "output_tokens")
            if getattr(response, "stop_reason", None) != "tool_use":
                break
            messages.append({"role": "assistant", "content": response.content})
            tool_results = await self._run_tools(response, patient_id, fetched)
            tool_calls += len(tool_results)
            messages.append({"role": "user", "content": tool_results})

        return self._build_answer(
            _extract_text(response),
            fetched,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            tool_calls=tool_calls,
        )

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
        self,
        text: str,
        fetched: dict[tuple[str, str], dict[str, Any]],
        *,
        input_tokens: int,
        output_tokens: int,
        tool_calls: int,
    ) -> AgentAnswer:
        # Usage is reported regardless of parse outcome — the tokens were spent
        # even when the model's reply couldn't be parsed into claims.
        payload = _parse_answer(text)
        if payload is None:
            # The model didn't return parseable JSON — markdown fences, or a prose
            # refusal to an out-of-scope question. Fail closed: no claims means the
            # chat service withholds with an honest message. A chat turn must never
            # 500 on a model formatting quirk.
            return AgentAnswer(
                answer="",
                claims=[],
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                tool_calls=tool_calls,
            )

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
                        resource_type=rtype,
                        resource_id=c.resource_id,
                        field=field,
                        value=value,
                        timestamp=extract_temporal(resource),
                    ),
                )
            )
        return AgentAnswer(
            answer=payload.answer,
            claims=claims,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            tool_calls=tool_calls,
        )


def _with_worker_context(
    message: str,
    guideline_evidence: list[GuidelineEvidence] | None,
    document_facts: list[ExtractedFact] | None,
) -> str:
    """``message`` plus the graph workers' output as labelled prompt context.

    With no worker output — every inline (flag-OFF) call — the message is
    returned unchanged, so the prompt is byte-for-byte what it was before.

    The blocks are explicit that neither source is citable: claims must still
    name a resource a tool returned, which is what keeps every ``source_ref``
    grounded in a live FHIR read.
    """
    blocks: list[str] = []
    if document_facts:
        lines = render_document_facts(document_facts)
        if lines:
            blocks.append(
                "Facts the intake-extractor read from the document(s) attached to this "
                "question. They are NOT FHIR resources — use them to inform your prose, "
                "but never cite one as a claim source:\n" + "\n".join(f"- {line}" for line in lines)
            )
    if guideline_evidence:
        lines = render_guideline_evidence(guideline_evidence)
        if lines:
            blocks.append(
                "Guideline excerpts the evidence-retriever retrieved for this question. "
                "They are general recommendations, NOT this patient's data — use them to "
                "inform your prose, but never cite one as a claim source:\n"
                + "\n".join(f"- {line}" for line in lines)
            )
    if not blocks:
        return message
    return message + "\n\n" + "\n\n".join(blocks)


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


def _usage_tokens(response: Any, field: str) -> int:
    """Read one integer counter off an Anthropic response's ``usage``.

    Defensive: a response with no ``usage`` (or a non-int counter) contributes
    zero, so a fake client or a partial response never breaks the token tally.
    """
    usage = getattr(response, "usage", None)
    value = getattr(usage, field, None)
    return value if isinstance(value, int) else 0


def _extract_text(response: Any) -> str:
    """Join the text parts of an Anthropic response's content blocks."""
    parts: list[str] = []
    for block in getattr(response, "content", []) or []:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts)
