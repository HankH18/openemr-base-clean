"""Supervisor + the hand-rolled multi-agent graph orchestrator.

The supervisor is a deterministic router: a document in scope goes to the
intake-extractor, an explicit guideline need to the evidence-retriever, both
signals reach both workers, and a plain chart question reaches neither (the
chart-only answer path). :class:`AgentGraph` drives one worker dispatch per
iteration, logs every supervisor<->agent transition as a typed
:class:`~copilot.graph.contracts.Handoff`, opens a nested observability span per
agent (worker spans are children of the supervisor span), captures the seven
observability fields, and finalizes through the critic + the deterministic
serve-time verifier — returning the unchanged Week-1
:class:`~copilot.domain.contracts.VerificationResult` inside a
:class:`~copilot.graph.contracts.GraphResult`.

A hard ``max_iterations`` cap (one worker dispatch = one iteration) that stops a
run before its planned grounding completes yields the safe "insufficient
grounded information" withhold — never an ungrounded answer, never an exception.
The chat service keeps its own "no grounded claims → withheld" override; this
graph does not duplicate it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from time import perf_counter
from typing import Protocol

from copilot.agent.factory import build_agent
from copilot.config import Settings
from copilot.domain.contracts import VerificationAction, VerificationResult
from copilot.domain.primitives import PatientId
from copilot.fhir.client import FhirClient
from copilot.fhir.provider import build_fhir_client
from copilot.graph.contracts import AgentTask, CriticVerdict, GraphMetrics, GraphResult, Handoff
from copilot.graph.critic import Critic
from copilot.graph.evidence_retriever import EvidenceRetriever
from copilot.graph.intake_extractor import IntakeExtractor
from copilot.observability import NoopObservability, Observability, Span
from copilot.observability.pricing import cost_usd
from copilot.verification.serve import verify_answer

_logger = logging.getLogger(__name__)

# Agent identities used as handoff endpoints + span-name stems. The span names
# and to_agent labels are what the trace reconstructs routing from, so keep the
# "intake"/"evidence"/"retriev"/"supervisor" stems stable.
_SUPERVISOR = "supervisor"
_INTAKE_AGENT = "intake-extractor"
_EVIDENCE_AGENT = "evidence-retriever"
_CRITIC_AGENT = "critic"

# Routing signals.
_INTAKE = "intake"
_EVIDENCE = "evidence"
_EVIDENCE_KEYWORDS = ("guideline", "recommend")

# The honest, evidence-free reply when a run cannot finish grounding safely.
_INSUFFICIENT_ANSWER = (
    "Insufficient grounded information to answer safely — withholding this answer."
)

# Sentinel for "no iteration cap" (build_graph max_iterations=None).
_UNLIMITED = 1_000_000_000


class Supervisor(Protocol):
    """The swappable routing surface (a deterministic router behind this Protocol)."""

    def route(self, task: AgentTask) -> list[str]: ...


class StubSupervisor:
    """Deterministic keyless router — pure signal detection, no model call."""

    def route(self, task: AgentTask) -> list[str]:
        plan: list[str] = []
        if task.document_ids:
            plan.append(_INTAKE)
        question = task.question.lower()
        if any(keyword in question for keyword in _EVIDENCE_KEYWORDS):
            plan.append(_EVIDENCE)
        return plan


def build_supervisor(settings: Settings) -> Supervisor:
    """Build the supervisor. Deterministic routing needs no key, but the
    factory keeps the same keyed shape as the workers for symmetry."""
    del settings  # routing is deterministic; no key-dependent behaviour today
    return StubSupervisor()


@dataclass(frozen=True)
class _FinalizeOutcome:
    answer: str
    verification: VerificationResult
    critic: CriticVerdict
    total_tokens: int
    cost_usd: float


class AgentGraph:
    """The supervisor-driven multi-agent graph.

    Collaborators are injected (Stub/Real chosen by the factory); the graph
    itself is provider-agnostic and runs identically keyed or keyless.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        supervisor: Supervisor,
        intake_extractor: IntakeExtractor,
        evidence_retriever: EvidenceRetriever,
        critic: Critic,
        observability: Observability | None = None,
        max_iterations: int | None = None,
        fhir_client_factory: Callable[[], FhirClient] | None = None,
    ) -> None:
        self._settings = settings
        self._supervisor = supervisor
        self._intake = intake_extractor
        self._evidence = evidence_retriever
        self._critic = critic
        self._obs: Observability = observability or NoopObservability()
        self._max_iterations = max_iterations if max_iterations is not None else _UNLIMITED
        self._fhir_client_factory = fhir_client_factory

    async def run(self, task: AgentTask) -> GraphResult:
        """Route, dispatch, gate, and verify — returning a typed graph result."""
        started = perf_counter()
        handoffs: list[Handoff] = []
        retrieval_hits = 0
        extraction_confidence = 0.0

        # Outer trace span; the supervisor span is its child (entered second) so
        # worker spans (children of the supervisor span) reconstruct from the
        # correlation id alone.
        async with (
            self._obs.span("graph.run", patient_id=task.patient_id),
            self._obs.span(
                "supervisor.route", patient_id=task.patient_id, question=task.question
            ) as supervisor_span,
        ):
            plan = self._supervisor.route(task)
            supervisor_span.set_attribute("route_plan", plan)

            dispatched: list[str] = []
            capped = False
            for target in plan:
                if len(dispatched) >= self._max_iterations:
                    capped = True
                    break
                if target == _INTAKE:
                    extraction_confidence = await self._dispatch_intake(task, handoffs)
                elif target == _EVIDENCE:
                    retrieval_hits = await self._dispatch_evidence(task, handoffs)
                dispatched.append(target)

            if capped:
                answer = _INSUFFICIENT_ANSWER
                verification = VerificationResult(
                    passed=False, claims=[], action=VerificationAction.withheld
                )
                critic_verdict: CriticVerdict | None = None
                total_tokens = 0
                cost = 0.0
            else:
                outcome = await self._finalize(task, handoffs)
                answer = outcome.answer
                verification = outcome.verification
                critic_verdict = outcome.critic
                total_tokens = outcome.total_tokens
                cost = outcome.cost_usd

            metrics = GraphMetrics(
                latency_ms=(perf_counter() - started) * 1000.0,
                total_tokens=total_tokens,
                cost_usd=cost,
                retrieval_hits=retrieval_hits,
                extraction_confidence=extraction_confidence,
                eval_outcome=verification.action.value,
                handoff_sequence=[f"{h.from_agent}->{h.to_agent}" for h in handoffs],
            )
            self._record_metrics(supervisor_span, metrics)
            self._obs.record_verification(
                passed=verification.passed,
                action=verification.action.value,
                patient_id=task.patient_id,
            )

        return GraphResult(
            answer=answer,
            verification=verification,
            handoffs=handoffs,
            metrics=metrics,
            critic=critic_verdict,
        )

    # --- worker dispatch --------------------------------------------------

    async def _dispatch_intake(self, task: AgentTask, handoffs: list[Handoff]) -> float:
        """Hand off to the intake-extractor; return its extraction confidence."""
        self._log_handoff(
            handoffs,
            _INTAKE_AGENT,
            "document(s) in scope — extract structured facts",
            {"document_ids": task.document_ids},
        )
        async with self._obs.span(
            "intake-extractor.run", document_ids=task.document_ids
        ) as span:
            report = await self._intake.run(task)
            span.set_attribute("extraction_confidence", report.extraction_confidence)
            span.set_attribute("fact_count", report.fact_count)
            span.set_output(
                {
                    "fact_count": report.fact_count,
                    "extraction_confidence": report.extraction_confidence,
                }
            )
        return report.extraction_confidence

    async def _dispatch_evidence(self, task: AgentTask, handoffs: list[Handoff]) -> int:
        """Hand off to the evidence-retriever; return its retrieval-hit count."""
        self._log_handoff(
            handoffs,
            _EVIDENCE_AGENT,
            "guideline need — retrieve supporting evidence",
            {"question": task.question},
        )
        async with self._obs.span(
            "evidence-retriever.retrieve", question=task.question
        ) as span:
            report = await self._evidence.run(task)
            span.set_attribute("retrieval_hits", report.hits)
            span.set_output({"retrieval_hits": report.hits})
        return report.hits

    # --- finalize (draft -> critic -> deterministic verifier) -------------

    async def _finalize(self, task: AgentTask, handoffs: list[Handoff]) -> _FinalizeOutcome:
        """Draft a grounded answer, gate it through the critic, then verify.

        Reuses the Week-1 chat agent + serve-time verifier verbatim so the
        returned :class:`VerificationResult` is the exact chat-service contract.
        """
        self._log_handoff(
            handoffs,
            _CRITIC_AGENT,
            "draft grounded — gate claims and run the deterministic verifier",
            {},
        )
        patient_id = PatientId(value=task.patient_id)
        async with self._obs.span("finalize.verify", patient_id=task.patient_id) as span:
            async with self._fhir_client() as fhir:
                agent = build_agent(self._settings, fhir)
                agent_answer = await agent.answer(patient_id, task.question, None)
                verification = await verify_answer(agent_answer.claims, patient_id, fhir)

            verdict = self._critic.review(list(agent_answer.claims))
            input_tokens = agent_answer.input_tokens or 0
            output_tokens = agent_answer.output_tokens or 0
            if agent_answer.input_tokens is None or agent_answer.output_tokens is None:
                cost = 0.0
            else:
                cost = cost_usd(
                    self._settings.anthropic_model_synthesis, input_tokens, output_tokens
                )

            span.set_attribute("critic_accepted", len(verdict.accepted))
            span.set_attribute("critic_rejected", len(verdict.rejected))
            span.set_attribute("eval_outcome", verification.action.value)
            span.set_output(
                {"action": verification.action.value, "claims": len(verification.claims)}
            )

        return _FinalizeOutcome(
            answer=agent_answer.answer,
            verification=verification,
            critic=verdict,
            total_tokens=input_tokens + output_tokens,
            cost_usd=cost,
        )

    # --- collaborators ----------------------------------------------------

    def _log_handoff(
        self, handoffs: list[Handoff], to_agent: str, reason: str, payload: dict[str, object]
    ) -> None:
        """Record + emit one supervisor->agent handoff (typed + logged)."""
        handoff = Handoff(
            from_agent=_SUPERVISOR, to_agent=to_agent, reason=reason, payload=payload
        )
        self._obs.event(
            "worker.handoff",
            from_agent=handoff.from_agent,
            to_agent=handoff.to_agent,
            reason=handoff.reason,
            payload=handoff.payload,
        )
        handoffs.append(handoff)

    def _record_metrics(self, span: Span, metrics: GraphMetrics) -> None:
        """Materialize the seven observability fields onto the span + an event."""
        span.set_attribute("latency_ms", metrics.latency_ms)
        span.set_attribute("total_tokens", metrics.total_tokens)
        span.set_attribute("cost_usd", metrics.cost_usd)
        span.set_attribute("retrieval_hits", metrics.retrieval_hits)
        span.set_attribute("extraction_confidence", metrics.extraction_confidence)
        span.set_attribute("eval_outcome", metrics.eval_outcome)
        span.set_output(metrics.model_dump())
        self._obs.event(
            "graph.telemetry",
            handoff_sequence=metrics.handoff_sequence,
            latency_ms=metrics.latency_ms,
            total_tokens=metrics.total_tokens,
            cost_usd=metrics.cost_usd,
            retrieval_hits=metrics.retrieval_hits,
            extraction_confidence=metrics.extraction_confidence,
            eval_outcome=metrics.eval_outcome,
        )

    def _fhir_client(self) -> FhirClient:
        if self._fhir_client_factory is not None:
            return self._fhir_client_factory()
        return build_fhir_client(self._settings)
