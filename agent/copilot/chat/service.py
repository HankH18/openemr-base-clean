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

In graph mode a SECOND gate runs after the verifier: the graph's critic. The
order is the safety property — the deterministic verifier is authoritative and
runs first, then the critic's verdict narrows what survived (see
``_critic_narrowed``). The critic is demote-only: it can drop a claim (uncited,
or flagged unsafe/narratively-inconsistent by the keyed safety pass) but can
never add or resurrect one. A verdict that rejects everything lands in the same
"nothing grounded ⇒ withheld" policy above rather than a new state.

Known limitation (pre-existing, not introduced by the critic gate): dropping a
claim removes it from the served evidence and from the HIPAA access trail, but
the answer PROSE is the agent's and may still narrate a dropped claim — exactly
as the verifier's ``degraded`` path already does. Prose regeneration is not
wired here.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from pydantic import BaseModel, ConfigDict, Field

from copilot.agent.base import AgentAnswer, ConversationTurn
from copilot.agent.factory import build_agent
from copilot.config import Settings
from copilot.domain.contracts import Claim, VerificationAction, VerificationResult
from copilot.domain.primitives import ClinicianId, FhirReference, PatientId
from copilot.fhir.client import FhirClient
from copilot.fhir.provider import build_fhir_client
from copilot.graph.contracts import AgentTask, CriticVerdict, Handoff
from copilot.graph.factory import build_graph
from copilot.memory.db import session_scope
from copilot.memory.records import ConversationMessage
from copilot.memory.repository import MemoryRepository
from copilot.observability import NoopObservability, Observability, Span, current_correlation_id
from copilot.observability.pricing import cost_usd
from copilot.rag import GuidelineEvidence
from copilot.verification.serve import verify_answer

_logger = logging.getLogger(__name__)

# Shown whenever nothing groundable backs the answer — the honest,
# evidence-free reply that surfaces uncertainty instead of guessing.
_WITHHELD_ANSWER = "I can't confirm that from this patient's record."

# Step names for the inline path's recorded tool/step sequence (req 7). Stable
# strings: a trace consumer keys on them.
_STEP_AGENT = "chat-agent.answer"
_STEP_VERIFY = "serve-verifier.verify_answer"


class _TurnOutcome(BaseModel):
    """What one grounded-answer path produced, before persistence + shaping.

    The two paths (inline / graph) return this same shape; the graph-only fields
    default to the inline path's "not applicable", so the flag-OFF default is
    unchanged.
    """

    model_config = ConfigDict(frozen=True)

    answer: str
    claims: list[Claim]
    action: VerificationAction
    passed: bool
    guideline_evidence: list[GuidelineEvidence] | None = None
    handoffs: list[Handoff] = Field(default_factory=list)


class ChatReply(BaseModel):
    """The assembled result of one chat turn, ready for HTTP shaping.

    ``guideline_evidence`` distinguishes three states, and the ``None`` vs ``[]``
    difference is load-bearing for the caller:

    - ``None`` — this turn ran the inline path, which does not retrieve. The
      route is responsible for retrieving the evidence block itself.
    - ``[]`` — the graph ran and the supervisor did not route to the
      evidence-retriever (no guideline need in the question), so there is
      genuinely no evidence for this turn.
    - non-empty — exactly the chunks the evidence-retriever worker retrieved.

    ``handoffs`` is the graph's ordered agent-transition log (empty on the inline
    path, which has no agents to hand off between).
    """

    model_config = ConfigDict(frozen=True)

    answer: str
    claims: list[Claim]
    action: VerificationAction
    passed: bool
    conversation_id: int
    correlation_id: str
    guideline_evidence: list[GuidelineEvidence] | None = None
    handoffs: list[Handoff] = Field(default_factory=list)


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
        document_ids: list[str] | None = None,
    ) -> ChatReply:
        """Answer ``message`` about ``patient_id``, grounded and fail-closed.

        Opens (or continues) the conversation, replays its history, produces a
        grounded answer, gates the claims against a live re-fetch, persists both
        the user turn and the assistant turn, and returns the assembled reply.

        The grounded answer is produced one of two ways, selected by the
        ``chat_graph_enabled`` flag (default OFF): the inline agent+verify path,
        or the hand-rolled multi-agent graph. Both apply the identical
        fail-closed reply invariant; persistence, audit, and the reply shape are
        shared and mode-independent. ``document_ids`` (graph mode only) puts
        already-ingested source documents in scope for the intake-extractor; the
        inline path ignores it.
        """
        async with session_scope() as session:
            repo = MemoryRepository(session)
            resolved_id = await self._resolve_conversation(
                repo, clinician_id, patient_id, correlation_id, conversation_id
            )
            history = _to_turns(await repo.get_conversation_messages(resolved_id))

        if self._settings.chat_graph_enabled:
            outcome = await self._answer_via_graph(
                patient_id, message, history, document_ids or []
            )
        else:
            outcome = await self._answer_inline(clinician_id, patient_id, message, history)

        async with session_scope() as session:
            repo = MemoryRepository(session)
            await repo.append_message(resolved_id, "user", message)
            await repo.append_message(resolved_id, "assistant", outcome.answer)

        # HIPAA §164.312(b): every PHI read leaves an append-only trail.
        await self._record_read_audit(clinician_id, patient_id, outcome.claims)

        return ChatReply(
            answer=outcome.answer,
            claims=outcome.claims,
            action=outcome.action,
            passed=outcome.passed,
            conversation_id=resolved_id,
            correlation_id=correlation_id,
            guideline_evidence=outcome.guideline_evidence,
            handoffs=outcome.handoffs,
        )

    # --- grounded-answer paths --------------------------------------------

    async def _answer_inline(
        self,
        clinician_id: ClinicianId,
        patient_id: PatientId,
        message: str,
        history: list[ConversationTurn],
    ) -> _TurnOutcome:
        """The inline agent+verify path — this service owns span + telemetry.

        Behaviour-preserving: identical spans, verification event, and token
        usage as before the graph flag existed (the flag-OFF default path). The
        observability fields added since are span ATTRIBUTES only — the inline
        path still emits no events of its own beyond ``llm.usage``.
        """
        async with self._obs.span(
            "chat", patient_id=patient_id.value, clinician_id=clinician_id.value
        ) as span:
            # The ordered steps this turn actually ran, recorded as they run —
            # the inline path's answer to req-7's "tool sequence" (a sequence,
            # not the bare count it used to report). The agent's own inner FHIR
            # tool invocations are counted, not named: AgentAnswer carries only
            # `tool_calls`, so naming them here would be invention.
            tool_sequence: list[str] = []
            async with self._fhir_client() as fhir:
                agent = build_agent(self._settings, fhir)
                tool_sequence.append(_STEP_AGENT)
                agent_answer = await agent.answer(patient_id, message, history)
                tool_sequence.append(_STEP_VERIFY)
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
            span.set_attribute("tool_sequence", tool_sequence)
            span.set_attribute("eval_outcome", action.value)
            # Token usage + computed USD cost onto the same span, so the trace
            # answers "how many tokens, at what cost". LLM path only.
            self._record_token_usage(span, agent_answer)
            span.set_output({"action": action.value, "passed": passed, "claims": len(claims)})

        # guideline_evidence=None: the inline path does not retrieve — the route
        # owns the evidence block for this mode (see ChatReply).
        return _TurnOutcome(answer=answer, claims=claims, action=action, passed=passed)

    async def _answer_via_graph(
        self,
        patient_id: PatientId,
        message: str,
        history: list[ConversationTurn],
        document_ids: list[str],
    ) -> _TurnOutcome:
        """The multi-agent graph path (``chat_graph_enabled``).

        Division of ownership: in graph mode the graph owns verification and
        telemetry recording — it opens its own trace spans, calls
        ``record_verification`` exactly once inside ``run()``, and emits its own
        token/cost telemetry. So this service records neither a second
        verification event nor token usage here; it only reshapes the graph's
        result into the fail-closed reply. Persistence + audit + reply shape are
        owned by ``chat`` and stay identical to the inline path.

        Two gates run here, in this order, and the order is the safety property:
        the deterministic verifier is authoritative and runs first (inside the
        graph), then the critic's verdict narrows what survived. The critic can
        only ever take claims away — see :func:`_critic_narrowed`.
        """
        graph = build_graph(
            self._settings,
            observability=self._obs,
            fhir_client_factory=self._fhir_client_factory,
        )
        task = AgentTask(
            patient_id=patient_id.value,
            question=message,
            document_ids=document_ids,
            history=history,
        )
        result = await graph.run(task)
        verification = result.verification

        # The verifier's survivors, then narrowed by the critic's verdict — a
        # claim the critic rejected (uncited, or flagged unsafe by the keyed
        # safety pass) is NOT served. Computing a rejection and serving the claim
        # anyway would make the critic decorative.
        claims = _critic_narrowed(_passed_claims(verification), result.critic)

        # Identical fail-closed invariant to the inline path: an answer that
        # grounded no verified claims is withheld, never served, regardless of
        # the verifier's empty-claims convenience. `not claims` folds the
        # critic's all-rejected case into that same existing policy rather than
        # inventing a state: nothing survived both gates, so there is nothing we
        # can prove — which is exactly what "withheld" already means.
        if not verification.claims or not claims:
            action = VerificationAction.withheld
            passed = False
        else:
            action = verification.action
            passed = verification.passed

        if action == VerificationAction.withheld:
            answer = _WITHHELD_ANSWER
            claims = []
        else:
            answer = result.answer

        return _TurnOutcome(
            answer=answer,
            claims=claims,
            action=action,
            passed=passed,
            guideline_evidence=result.guideline_evidence,
            handoffs=result.handoffs,
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

        Only fhir-cited claims contribute: the row records which *FHIR* records
        this read touched, and a document/guideline citation names an agent-store
        row, not a FHIR resource — listing its id here would misreport the PHI
        access trail.
        """
        try:
            async with session_scope() as session:
                await MemoryRepository(session).record_audit(
                    correlation_id=current_correlation_id(),
                    action="chat",
                    patient_id=patient_id,
                    clinician_id=clinician_id.value,
                    resources_returned=[
                        claim.source_ref.resource_id
                        for claim in claims
                        if isinstance(claim.source_ref, FhirReference)
                    ],
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


def _critic_narrowed(claims: list[Claim], verdict: CriticVerdict | None) -> list[Claim]:
    """The verifier-passed ``claims`` the critic also accepted — DEMOTE-ONLY.

    Structurally incapable of resurrecting a claim: it FILTERS the list the
    deterministic verifier already passed rather than reading ``verdict.accepted``
    as a source of claims. A text the verifier dropped is simply not in ``claims``,
    so no verdict — however wrong, however adversarial the model behind it — can
    put it back. The verifier stays authoritative; the critic only narrows.

    Subtracting ``rejected`` matters when two claims share a text (the verdict is
    keyed by text, so such a pair is genuinely ambiguous): the ambiguity resolves
    toward dropping, never toward serving something the critic rejected.

    ``None`` narrows nothing — the run never reached the critic (iteration cap),
    so there is no verdict to apply. Note a critic *error* never arrives here as
    ``None``: ``RealCritic.review`` fails safe to its deterministic partition,
    which accepts every cited claim, so an LLM outage degrades to the pre-existing
    citation gate rather than withholding the turn.
    """
    if verdict is None:
        return claims
    accepted = set(verdict.accepted) - set(verdict.rejected)
    return [claim for claim in claims if claim.text in accepted]
