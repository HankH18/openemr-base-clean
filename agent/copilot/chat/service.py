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

from pydantic import BaseModel, ConfigDict

from copilot.agent.base import ConversationTurn
from copilot.agent.factory import build_agent
from copilot.config import Settings
from copilot.domain.contracts import Claim, VerificationAction, VerificationResult
from copilot.domain.primitives import ClinicianId, PatientId
from copilot.fhir.client import FhirClient
from copilot.fhir.provider import build_fhir_client
from copilot.memory.db import session_scope
from copilot.memory.records import ConversationMessage
from copilot.memory.repository import MemoryRepository
from copilot.observability import NoopObservability, Observability
from copilot.verification.serve import verify_answer

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

    def __init__(self, settings: Settings, observability: Observability | None = None) -> None:
        self._settings = settings
        self._obs: Observability = observability or NoopObservability()

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
            span.set_output({"action": action.value, "passed": passed, "claims": len(claims)})

        async with session_scope() as session:
            repo = MemoryRepository(session)
            await repo.append_message(resolved_id, "user", message)
            await repo.append_message(resolved_id, "assistant", answer)

        return ChatReply(
            answer=answer,
            claims=claims,
            action=action,
            passed=passed,
            conversation_id=resolved_id,
            correlation_id=correlation_id,
        )

    # --- collaborators ----------------------------------------------------

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

        Real Backend Services token when configured, else a stub bearer — see
        ``copilot.fhir.provider.build_token_provider``. Both the agent's reads
        and the serve-time verifier's re-fetch share this client for the turn.
        """
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
