"""Typed contracts for the hand-rolled multi-agent graph (Week 2, F7).

The supervisor routes an :class:`AgentTask` to the intake-extractor and/or the
evidence-retriever workers, then finalizes through the critic and the
deterministic serve-time verifier. Every supervisor<->worker transition is a
typed :class:`Handoff`; the critic returns a :class:`CriticVerdict`; and the
whole run returns a :class:`GraphResult` that carries the unchanged Week-1
:class:`~copilot.domain.contracts.VerificationResult` the chat service consumes.

These are the pinned public contracts (see ``W2_ARCHITECTURE.md`` §Graph); the
worker report DTOs live beside their workers.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from copilot.agent.base import ConversationTurn
from copilot.domain.contracts import VerificationResult
from copilot.rag import GuidelineEvidence


class AgentTask(BaseModel):
    """One unit of work handed to the graph.

    ``patient_id`` is the OpenEMR PID the question is scoped to, ``question`` the
    free-text ask, and ``document_ids`` the already-ingested source-document row
    ids (as strings) in scope — their presence is what routes work to the
    intake-extractor. ``history`` replays the prior conversation turns the chat
    service resolved; it defaults empty so a document/guideline task built
    without a thread is unchanged.
    """

    model_config = ConfigDict(frozen=True)

    patient_id: int = Field(gt=0)
    question: str = Field(min_length=1)
    document_ids: list[str] = Field(default_factory=list)
    history: list[ConversationTurn] = Field(default_factory=list)


class Handoff(BaseModel):
    """One typed, logged transition between two graph agents.

    Emitted into the trace as a ``worker.handoff`` event (with these four fields
    as attributes) at the moment the supervisor dispatches a worker or the
    critic. ``payload`` carries NON-PHI routing signals only — document ids,
    counts, and terms from the module's fixed routing vocabulary. Never the
    clinician's question, and never any other free text read from the record.

    This sentence used to read "the routing context (document ids, the query, …)
    — always a mapping, never free text", which named the query and denied free
    text in the same breath. The payload really did carry the raw question, and
    this event really does egress to Langfuse, so the docstring was not merely
    stale: it described the leak and asserted its absence, which is how a reader
    checking for PHI egress would have been talked out of looking. Keep this
    accurate; the trace is a third-party surface.
    """

    model_config = ConfigDict(frozen=True)

    from_agent: str = Field(min_length=1)
    to_agent: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)


class CriticVerdict(BaseModel):
    """The critic's accept/reject partition of drafted claims — SERVE-AFFECTING.

    ``accepted`` holds the claim texts that carry a machine-readable citation
    (and that the keyed safety pass did not flag as unsafe/inconsistent);
    ``rejected`` the texts of the rest.

    The critic AUGMENTS — it never replaces — the deterministic verifier, and the
    order is fixed: the verifier runs first and is authoritative, then the chat
    service intersects the verifier-passed claims with ``accepted`` (see
    ``copilot.chat.service._critic_narrowed``). So this verdict is DEMOTE-ONLY —
    it can drop a claim from the served answer but can never add or resurrect one
    the verifier already rejected. It is not advisory: a rejected claim is not
    served.
    """

    model_config = ConfigDict(frozen=True)

    accepted: list[str] = Field(default_factory=list)
    rejected: list[str] = Field(default_factory=list)
    #: The subset of ``rejected`` the keyed safety pass flagged as ``unsafe_action``
    #: — i.e. the claim's PROSE recommends something clinically unsafe.
    #:
    #: This is separated from ``rejected`` because the two need different remedies.
    #: A ``narrative_inconsistency`` is contained by dropping the claim: the answer
    #: loses an unsupported assertion and the rest stands. An ``unsafe_action`` is
    #: not — the danger lives in the sentence the model wrote, so removing the
    #: claim only strips the evidence while the unsafe suggestion still reaches the
    #: physician, now unfootnoted. The chat service therefore WITHHOLDS the whole
    #: answer when this is non-empty (see ``copilot.chat.service``), which is the
    #: same fail-closed reflex the rest of the system uses: if we cannot serve it
    #: safely, we do not serve it.
    #:
    #: Empty for ``StubCritic`` and for every deterministic partition, so the
    #: keyless path is unaffected.
    unsafe: list[str] = Field(default_factory=list)


class GraphMetrics(BaseModel):
    """The seven observability fields captured for one graph run.

    Materialized onto the supervisor span, the ``graph.telemetry`` event, and
    this result so a single run's trace answers: what did each agent hand off,
    how long did it take, how many tokens at what cost, how many retrieval hits,
    how confident was the extraction, and what was the eval (verification)
    outcome.
    """

    model_config = ConfigDict(frozen=True)

    latency_ms: float = Field(ge=0.0)
    total_tokens: int = Field(ge=0)
    cost_usd: float = Field(ge=0.0)
    retrieval_hits: int = Field(ge=0)
    extraction_confidence: float = Field(ge=0.0)
    eval_outcome: str
    handoff_sequence: list[str] = Field(default_factory=list)


class GraphResult(BaseModel):
    """What ``graph.run(task)`` returns.

    ``verification`` is the unchanged Week-1 :class:`VerificationResult` the chat
    service consumes — the graph preserves that contract rather than inventing a
    new one. ``answer`` is the drafted prose, ``handoffs`` the ordered typed
    transition log, ``metrics`` the observability fields, and ``critic`` the
    demote-only verdict when the run reached finalize (``None`` when the
    iteration cap stopped the run before it).

    ``guideline_evidence`` is exactly what the evidence-retriever worker
    retrieved under the supervisor's routing decision — the same chunks that
    informed the prose. Surfacing it here is what lets the chat route DISPLAY the
    supervisor's evidence instead of retrieving a second, decoupled set of its
    own (one retrieval per turn).

    An empty ``guideline_evidence`` list has TWO causes that must not be
    conflated, and ``evidence_retrieved`` is the discriminator:

    - ``evidence_retrieved is False`` — the supervisor did not dispatch the
      evidence worker (no guideline need in the question). Honest "this turn
      needed no guideline evidence".
    - ``evidence_retrieved is True`` — the worker RAN and retrieved zero chunks
      (an empty/degraded corpus, or a query that legitimately matched nothing).
      Materially different from "no guideline need": "we looked and found none".

    ``evidence_retrieved`` is ``evidence_report is not None`` — i.e. whether the
    evidence-retriever worker executed this turn. It never gates served/withheld
    (guideline evidence informs the prose only, never a Claim); it exists purely
    so a zero-hit retrieval is distinguishable from a never-routed turn.
    """

    model_config = ConfigDict(frozen=True)

    answer: str
    verification: VerificationResult
    handoffs: list[Handoff] = Field(default_factory=list)
    metrics: GraphMetrics
    critic: CriticVerdict | None = None
    guideline_evidence: list[GuidelineEvidence] = Field(default_factory=list)
    #: Whether the evidence-retriever worker ran this turn (``evidence_report is
    #: not None``). Splits routed-but-zero-hit (``True`` + empty
    #: ``guideline_evidence``) from never-routed (``False`` + empty).
    evidence_retrieved: bool = False
