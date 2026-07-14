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

from copilot.domain.contracts import VerificationResult


class AgentTask(BaseModel):
    """One unit of work handed to the graph.

    ``patient_id`` is the OpenEMR PID the question is scoped to, ``question`` the
    free-text ask, and ``document_ids`` the already-ingested source-document row
    ids (as strings) in scope — their presence is what routes work to the
    intake-extractor.
    """

    model_config = ConfigDict(frozen=True)

    patient_id: int = Field(gt=0)
    question: str = Field(min_length=1)
    document_ids: list[str] = Field(default_factory=list)


class Handoff(BaseModel):
    """One typed, logged transition between two graph agents.

    Emitted into the trace as a ``worker.handoff`` event (with these four fields
    as attributes) at the moment the supervisor dispatches a worker or the
    critic. ``payload`` carries the routing context (document ids, the query,
    …) — always a mapping, never free text.
    """

    model_config = ConfigDict(frozen=True)

    from_agent: str = Field(min_length=1)
    to_agent: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)


class CriticVerdict(BaseModel):
    """The critic's deterministic accept/reject partition of drafted claims.

    ``accepted`` holds the claim texts that carry a machine-readable citation;
    ``rejected`` the texts of claims that do not. The critic AUGMENTS — it never
    replaces — the deterministic verifier, so a verdict is advisory telemetry
    alongside the authoritative :class:`VerificationResult`.
    """

    model_config = ConfigDict(frozen=True)

    accepted: list[str] = Field(default_factory=list)
    rejected: list[str] = Field(default_factory=list)


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
    (advisory) verdict when the run reached finalize.
    """

    model_config = ConfigDict(frozen=True)

    answer: str
    verification: VerificationResult
    handoffs: list[Handoff] = Field(default_factory=list)
    metrics: GraphMetrics
    critic: CriticVerdict | None = None
