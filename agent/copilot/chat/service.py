"""Serve-time chat orchestration — grounded drill-down, fail-closed.

Keeps the route thin.  One place resolves (or opens) the conversation, replays
its history, runs the chat agent against a live FHIR reader, gates every claim
through the serve-time verifier (a live re-fetch by ID), persists the turn, and
assembles the reply.

The verifier owns the served/degraded/withheld decision — with one deliberate
override for the chat path: when the agent grounded *nothing* (an ungroundable
question, e.g. asking about an MRI that is not in the record), the reply is
``withheld`` with an honest message, never ``served``.  The verifier's
"no claims ⇒ served" convenience (so a memory file can still surface domain
flags) is the wrong default here: an answer with no evidence must not read as
confirmed.  A verifier ``withheld`` (claims existed but all failed the live
re-fetch — the record drifted) collapses to the same honest, evidence-free
reply.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from pydantic import BaseModel, ConfigDict

from copilot.agent.base import AgentAnswer, ConversationTurn
from copilot.agent.factory import build_agent
from copilot.config import Settings
from copilot.domain.contracts import Claim, VerificationAction, VerificationResult
from copilot.domain.primitives import ClinicianId, PatientId
from copilot.fhir.client import FhirClient
from copilot.fhir.provider import build_fhir_client
from copilot.memory.db import session_scope
from copilot.memory.records import ConversationMessage
from copilot.memory.repository import MemoryRepository
from copilot.observability import NoopObservability, Observability, Span, current_correlation_id
from copilot.observability.pricing import cost_usd
from copilot.verification.serve import verify_answer

_logger = logging.getLogger(__name__)

# Shown whenever nothing groundable backs the answer — the honest,
# evidence-free reply that surfaces uncertainty instead of guessing.
_WITHHELD_ANSWER = "I can't confirm that from this patient's record."


class ChatReply(BaseModel):
    """The assembled result of one chat turn, ready for HTTP shaping."""

    model_config = ConfigDict(frozen=True)

    answer: str
    claims: list[Claim]
    action: VerificationAction
    passed: bool
    conversation_id: int
    correlation_id: str


class ChatService:
    """Serve-time orchestration for a single grounded chat turn."""

    def __init__(
        self,
        settings: Settings,
        observability: Observability | None = None,
        *,
        fhir_client_factory: Callable[[], FhirClient] | None = None,
    ) -> None:
        self._settings = settings
        self._obs: Observability = observability or NoopObservability()
        # Optional per-request reader factory. In ``smart`` mode the route injects
        # a factory that builds the physician's delegated per-session client; when
        # absent (disabled mode) the client falls back to the system-token path.
        self._fhir_client_factory = fhir_client_factory

    async def chat(
        self,
        clinician_id: ClinicianId,
        patient_id: PatientId,
        message: str,
        correlation_id: str,
        conversation_id: int | None = None,
    ) -> ChatReply:
        """Answer ``message`` about ``patient_id``, grounded and fail-closed.

        Opens (or continues) the conversation, replays its history, produces a
        grounded answer, gates the claims against a live re-fetch, persists both
        the user turn and the assistant turn, and returns the assembled reply.
        """
        async with session_scope() as session:
            repo = MemoryRepository(session)
            resolved_id = await self._resolve_conversation(
                repo, clinician_id, patient_id, correlation_id, conversation_id
            )
            history = _to_turns(await repo.get_conversation_messages(resolved_id))

        async with self._obs.span(
            "chat", patient_id=patient_id.value, clinician_id=clinician_id.value
        ) as span:
            async with self._fhir_client() as fhir:
                agent = build_agent(self._settings, fhir)
                agent_answer = await agent.answer(patient_id, message, history)
                result = await verify_answer(agent_answer.claims, patient_id, fhir)

            # Fail-closed: an answer that grounded nothing is withheld, never
            # served, regardless of the verifier's empty-claims convenience.
            if not agent_answer.claims:
                action = VerificationAction.withheld
                passed = False
            else:
                action = result.action
                passed = result.passed

            if action == VerificationAction.withheld:
                answer = _WITHHELD_ANSWER
                claims: list[Claim] = []
            else:
                answer = agent_answer.answer
                claims = _passed_claims(result)

            # One verification event per served/withheld decision — the
            # fail-closed metric the observability dashboard tracks.
            self._obs.record_verification(
                passed=passed, action=action.value, patient_id=patient_id.value
            )
            # Token usage + computed USD cost onto the same span, so the trace
            # answers "how many tokens, at what cost". LLM path only.
            self._record_token_usage(span, agent_answer)
            span.set_output({"action": action.value, "passed": passed, "claims": len(claims)})

        async with session_scope() as session:
            repo = MemoryRepository(session)
            await repo.append_message(resolved_id, "user", message)
            await repo.append_message(resolved_id, "assistant", answer)

        # HIPAA §164.312(b): every PHI read leaves an append-only trail.
        await self._record_read_audit(clinician_id, patient_id, claims)

        return ChatReply(
            answer=answer,
            claims=claims,
            action=action,
            passed=passed,
            conversation_id=resolved_id,
            correlation_id=correlation_id,
        )

    # --- collaborators ----------------------------------------------------

    def _record_token_usage(self, span: Span, answer: AgentAnswer) -> None:
        """Record LLM token usage + computed USD cost onto the chat span.

        Only the LLM path reports usage; the deterministic stub agent leaves the
        counts unset (``None``), in which case there is nothing to cost and we
        record nothing. Both a span attribute and a one-off event are emitted so
        the trace and the flat event stream each answer "how many tokens, at
        what cost".
        """
        if answer.input_tokens is None or answer.output_tokens is None:
            return
        model = self._settings.anthropic_model_synthesis
        cost = cost_usd(model, answer.input_tokens, answer.output_tokens)
        span.set_attribute("input_tokens", answer.input_tokens)
        span.set_attribute("output_tokens", answer.output_tokens)
        span.set_attribute("cost_usd", cost)
        span.set_attribute("tool_calls", answer.tool_calls)
        self._obs.event(
            "llm.usage",
            model=model,
            input_tokens=answer.input_tokens,
            output_tokens=answer.output_tokens,
            cost_usd=cost,
            tool_calls=answer.tool_calls,
            correlation_id=current_correlation_id(),
        )

    async def _record_read_audit(
        self, clinician_id: ClinicianId, patient_id: PatientId, claims: list[Claim]
    ) -> None:
        """Append the HIPAA access-trail row for this chat PHI read.

        Fail-open: the answer is already produced and returned to the
        clinician, so a failed audit write must never turn a served read into
        an error. The write runs in its own transaction; any failure is logged
        and swallowed. ``resources_returned`` is the set of FHIR resources the
        answer actually cited (empty when the turn was withheld).
        """
        try:
            async with session_scope() as session:
                await MemoryRepository(session).record_audit(
                    correlation_id=current_correlation_id(),
                    action="chat",
                    patient_id=patient_id,
                    clinician_id=clinician_id.value,
                    resources_returned=[claim.source_ref.resource_id for claim in claims],
                )
        except Exception:
            _logger.exception(
                "failed to write chat read audit row",
                extra={"patient_id": patient_id.value, "clinician_id": clinician_id.value},
            )

    async def _resolve_conversation(
        self,
        repo: MemoryRepository,
        clinician_id: ClinicianId,
        patient_id: PatientId,
        correlation_id: str,
        conversation_id: int | None,
    ) -> int:
        """Echo a supplied conversation id, or open a fresh patient-scoped one."""
        if conversation_id is not None:
            return conversation_id
        return await repo.create_conversation(clinician_id, patient_id, correlation_id)

    def _fhir_client(self) -> FhirClient:
        """Build the FHIR reader for a chat turn.

        Smart mode: the route-injected factory builds the physician's delegated
        per-session client, so OpenEMR attributes the read to that physician.
        Otherwise (disabled mode): the environment-appropriate system client —
        real Backend Services token when configured, else a stub bearer (see
        ``copilot.fhir.provider.build_token_provider``). Both the agent's reads
        and the serve-time verifier's re-fetch share this client for the turn.
        """
        if self._fhir_client_factory is not None:
            return self._fhir_client_factory()
        return build_fhir_client(self._settings)


# --- module helpers --------------------------------------------------------


def _to_turns(messages: list[ConversationMessage]) -> list[ConversationTurn]:
    """Replay persisted turns as agent context, keeping only chat roles."""
    turns: list[ConversationTurn] = []
    for m in messages:
        if m.role == "user":
            turns.append(ConversationTurn(role="user", content=m.content))
        elif m.role == "assistant":
            turns.append(ConversationTurn(role="assistant", content=m.content))
    return turns


def _passed_claims(result: VerificationResult) -> list[Claim]:
    """Rebuild the claims the verifier passed (attribution + value match)."""
    return [
        Claim(text=r.text, source_ref=r.source_ref)
        for r in result.claims
        if r.attribution_ok and r.value_match
    ]
