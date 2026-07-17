"""Supervisor + the hand-rolled multi-agent graph orchestrator.

The supervisor is a deterministic router: a document in scope goes to the
intake-extractor, guideline intent in the question (see
:func:`evidence_signals`) to the evidence-retriever, both signals reach both
workers, and a plain chart question reaches neither (the chart-only answer
path). :class:`AgentGraph` drives one worker dispatch per iteration, logs every
supervisor<->agent transition as a typed
:class:`~copilot.graph.contracts.Handoff`, opens a nested observability span per
agent (worker spans are children of the supervisor span), captures the seven
observability fields, and finalizes through the critic + the deterministic
serve-time verifier — returning the unchanged Week-1
:class:`~copilot.domain.contracts.VerificationResult` inside a
:class:`~copilot.graph.contracts.GraphResult`.

Worker output reaches the answer: :meth:`AgentGraph._finalize` hands the
retrieved guideline chunks and the extracted document facts to the answering
agent (keyword-only, defaulted arguments on
:class:`~copilot.agent.base.ChatAgent`), so dispatching a worker demonstrably
changes the reply. It informs the *prose* only — worker output never becomes a
:class:`~copilot.domain.contracts.Claim`, so the deterministic verifier's
FHIR re-fetch gate stays the sole authority on served/withheld.

A hard ``max_iterations`` cap (one worker dispatch = one iteration) that stops a
run before its planned grounding completes yields the safe "insufficient
grounded information" withhold — never an ungrounded answer, never an exception.
The chat service keeps its own "no grounded claims → withheld" override; this
graph does not duplicate it.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from time import perf_counter
from typing import Protocol

from anyio import to_thread

from copilot.agent.factory import build_agent
from copilot.config import Settings
from copilot.domain.contracts import Claim, VerificationAction, VerificationResult
from copilot.domain.primitives import PatientId
from copilot.fhir.client import FhirClient
from copilot.fhir.provider import build_fhir_client
from copilot.graph.contracts import AgentTask, CriticVerdict, GraphMetrics, GraphResult, Handoff
from copilot.graph.critic import Critic
from copilot.graph.evidence_retriever import EvidenceReport, EvidenceRetriever
from copilot.graph.intake_extractor import IntakeExtractor, IntakeReport
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

# --- guideline-intent vocabulary (deterministic; no model call) --------------
#
# Tier 1 — an explicit reference to a normative source. Any one of these routes
# to the evidence-retriever on its own.
_GUIDELINE_TERMS: tuple[str, ...] = (
    r"guidelines?",
    r"recommend\w*",
    r"standards? of care",
    r"per protocol",
    r"protocols?",
    r"evidence[\s-]based",
    r"best practices?",
    r"contraindicat\w*",
    r"first[\s-]line",
    r"indicated for",
    r"workup for",
    # A threshold/target is a normative quantity by definition — no FHIR
    # resource holds "the transfusion threshold" — so unlike "dose" (a real
    # chart field, hence tier 2) these name an evidence need on their own.
    r"thresholds?",
    r"targets?",
)

# Tier 2 — an appraisal cue ("is this X right?") ANDed with a clinical-decision
# noun. Neither half routes alone, which is what keeps a plain chart lookup
# ("what is this patient's current potassium?") on the chart-only path while
# "is this dose appropriate?" reaches the evidence-retriever. The AND is the
# whole point: "dose" alone is a lookup, "appropriate" alone is not clinical.
_APPRAISAL_CUES: tuple[str, ...] = (
    r"appropriate\w*",
    r"should",
    r"safe",
    r"unsafe",
    r"adequate\w*",
    r"acceptable",
    r"warranted",
    r"advisable",
    r"reasonable",
    r"correct",
    r"adjust\w*",
    r"too (?:high|low|much|little)",
)
_DECISION_NOUNS: tuple[str, ...] = (
    r"dos(?:e|es|ing|age)",
    r"regimens?",
    r"therap(?:y|ies)",
    r"treatment\w*",
    r"management",
    r"titrat\w*",
    r"anticoagulat\w*",
    r"antibiotics?",
    r"insulin",
    r"transfus\w*",
    r"monitor\w*",
)


def _compile(terms: tuple[str, ...]) -> tuple[re.Pattern[str], ...]:
    """Word-boundary-anchored patterns, so "target" never fires inside "targeted"
    by accident and each term stays readable at its definition site."""
    return tuple(re.compile(rf"\b{term}\b") for term in terms)


_GUIDELINE_PATTERNS = _compile(_GUIDELINE_TERMS)
_APPRAISAL_PATTERNS = _compile(_APPRAISAL_CUES)
_DECISION_PATTERNS = _compile(_DECISION_NOUNS)


def _found(question: str, patterns: tuple[re.Pattern[str], ...]) -> list[str]:
    """The literal text each pattern matched — the router's inspectable evidence."""
    return [match.group(0) for pattern in patterns if (match := pattern.search(question))]


def evidence_signals(question: str) -> list[str]:
    """The guideline-intent signals in ``question`` — empty means "no evidence need".

    Deterministic and inspectable by design: the returned strings are the exact
    words the router keyed on, so a routing decision can always be explained
    from the trace (they ride along on the evidence handoff's payload) rather
    than being an opaque model judgement.
    """
    lowered = question.lower()
    explicit = _found(lowered, _GUIDELINE_PATTERNS)
    if explicit:
        return explicit
    cues = _found(lowered, _APPRAISAL_PATTERNS)
    nouns = _found(lowered, _DECISION_PATTERNS)
    if cues and nouns:
        return [*cues, *nouns]
    return []

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
    """Deterministic keyless router — pure signal detection, no model call.

    Two rules, both inspectable: documents in scope (``document_ids``) are the
    intake-extractor's signal — the worker reads stored extractions by id, so
    ids are the only thing that can route it — and guideline intent in the
    question (see :func:`evidence_signals`) is the evidence-retriever's.
    Deliberately not an LLM: routing that can be explained from a word list is
    auditable, and a router that hallucinates a plan is a worse failure than one
    that misses a synonym.
    """

    def route(self, task: AgentTask) -> list[str]:
        plan: list[str] = []
        if task.document_ids:
            plan.append(_INTAKE)
        if evidence_signals(task.question):
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
        intake_report: IntakeReport | None = None
        evidence_report: EvidenceReport | None = None

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
                    intake_report = await self._dispatch_intake(task, handoffs)
                elif target == _EVIDENCE:
                    evidence_report = await self._dispatch_evidence(task, handoffs)
                dispatched.append(target)

            retrieval_hits = evidence_report.hits if evidence_report is not None else 0
            extraction_confidence = (
                intake_report.extraction_confidence if intake_report is not None else 0.0
            )

            if capped:
                answer = _INSUFFICIENT_ANSWER
                verification = VerificationResult(
                    passed=False, claims=[], action=VerificationAction.withheld
                )
                critic_verdict: CriticVerdict | None = None
                total_tokens = 0
                cost = 0.0
            else:
                outcome = await self._finalize(
                    task, handoffs, intake=intake_report, evidence=evidence_report
                )
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
            # The worker's own evidence rides out on the result so the chat route
            # can display it rather than retrieving a second, decoupled set —
            # one retrieval per turn, and what is shown is what the supervisor
            # actually decided to retrieve (and what informed the prose).
            guideline_evidence=list(evidence_report.evidence)
            if evidence_report is not None
            else [],
        )

    # --- worker dispatch --------------------------------------------------

    async def _dispatch_intake(self, task: AgentTask, handoffs: list[Handoff]) -> IntakeReport:
        """Hand off to the intake-extractor; return its full report.

        The whole report — not just the confidence — because finalize hands the
        extracted facts to the answering agent.
        """
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
        return report

    async def _dispatch_evidence(self, task: AgentTask, handoffs: list[Handoff]) -> EvidenceReport:
        """Hand off to the evidence-retriever; return its full report.

        The whole report — not just the hit count — because finalize hands the
        retrieved guideline chunks to the answering agent. The handoff payload
        carries the router's matched signals, so the trace explains *why* this
        worker was dispatched, not merely that it was.
        """
        self._log_handoff(
            handoffs,
            _EVIDENCE_AGENT,
            "guideline need — retrieve supporting evidence",
            {"question": task.question, "signals": evidence_signals(task.question)},
        )
        async with self._obs.span(
            "evidence-retriever.retrieve", question=task.question
        ) as span:
            report = await self._evidence.run(task)
            span.set_attribute("retrieval_hits", report.hits)
            span.set_output({"retrieval_hits": report.hits})
        return report

    # --- finalize (draft -> critic -> deterministic verifier) -------------

    async def _finalize(
        self,
        task: AgentTask,
        handoffs: list[Handoff],
        *,
        intake: IntakeReport | None,
        evidence: EvidenceReport | None,
    ) -> _FinalizeOutcome:
        """Draft an answer informed by the workers, gate it, then verify.

        This is where worker output earns its keep: the guideline chunks the
        evidence-retriever returned and the facts the intake-extractor read are
        handed to the answering agent, so a run that dispatched a worker cannot
        produce the same answer as one that didn't. When no worker ran, both are
        ``None`` and the agent call is byte-for-byte the inline path's.

        Worker output informs the prose only — it never becomes a
        :class:`~copilot.domain.contracts.Claim`. Claims stay FHIR-grounded, so
        the deterministic serve-time verifier remains the authority on
        served/withheld and its re-fetch gate is untouched by anything a worker
        found.
        """
        guideline_evidence = list(evidence.evidence) if evidence is not None else []
        document_facts = list(intake.facts) if intake is not None else []
        self._log_handoff(
            handoffs,
            _CRITIC_AGENT,
            "draft grounded — gate claims and run the deterministic verifier",
            {
                "guideline_evidence": len(guideline_evidence),
                "document_facts": len(document_facts),
            },
        )
        patient_id = PatientId(value=task.patient_id)
        async with self._obs.span("finalize.verify", patient_id=task.patient_id) as span:
            async with self._fhir_client() as fhir:
                agent = build_agent(self._settings, fhir)
                agent_answer = await agent.answer(
                    patient_id,
                    task.question,
                    task.history or None,
                    # None (not []) when a worker didn't run, so the agent sees
                    # exactly the inline path's arguments.
                    guideline_evidence=guideline_evidence or None,
                    document_facts=document_facts or None,
                )
                verification = await verify_answer(agent_answer.claims, patient_id, fhir)

            verdict = await self._review(agent_answer.claims)
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
            # What the workers actually contributed to this answer — the trace
            # can now distinguish an evidence-informed answer from a bare one.
            span.set_attribute("guideline_evidence_used", len(guideline_evidence))
            span.set_attribute("document_facts_used", len(document_facts))
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

    async def _review(self, claims: Sequence[Claim]) -> CriticVerdict:
        """Run the critic's gate WITHOUT blocking the event loop.

        ``Critic.review`` is a **synchronous** Protocol method, and the keyed
        :class:`~copilot.graph.critic.RealCritic` implements it with a
        synchronous Anthropic call. Invoking it inline from this coroutine (as
        this line used to) blocked the whole event loop for the duration of that
        network call — stalling *every* concurrent clinician's request behind one
        turn's safety pass. Offloading to a worker thread is what makes the
        blocking call cooperate with the loop again.

        **Why a thread rather than making the Protocol async.** Both fix the
        stall; the thread is the one that fixes it without collateral damage:

        - ``review`` is a Protocol with several implementors — ``StubCritic``,
          the fakes in ``tests/``, and the frozen acceptance harness. Making it
          ``async`` is a breaking contract change for all of them, and it would
          force the pure-Python, I/O-free ``StubCritic`` to become a coroutine
          purely because its keyed sibling happens to make a network call. That
          is the network detail leaking upward into the abstraction.
        - The fail-safe and demote-only invariants live *inside* ``review``
          (``RealCritic`` catches every exception and falls back to the
          deterministic partition). Running the identical method body on a
          different thread cannot perturb either one — whereas rewriting it
          around an async client would put both back in play for no gain here.

        ``to_thread.run_sync`` is cancellation-correct for our purposes: the
        thread is not abandoned mid-flight, and the underlying call is already
        bounded by ``GATING_TIMEOUT``, so a wedged critic releases its thread in
        seconds rather than never. ``StubCritic`` pays one thread hop (~tens of
        microseconds, once per turn) — an irrelevant cost next to the FHIR and
        model round-trips this method already awaits.
        """
        return await to_thread.run_sync(self._critic.review, list(claims))

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
