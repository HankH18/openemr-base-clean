"""Hand-rolled multi-agent graph (Week 2, F7).

A deterministic supervisor routes an :class:`AgentTask` to an intake-extractor
and/or an evidence-retriever worker, then finalizes through a critic and the
Week-1 serve-time verifier. Each worker + the critic is a Stub/Real dual behind
a ``typing.Protocol`` + ``build_*`` factory (keyless → stub), handoffs are typed
:class:`Handoff` objects logged into the trace, and the run returns the
unchanged :class:`~copilot.domain.contracts.VerificationResult` inside a
:class:`GraphResult`.
"""

from copilot.graph.contracts import (
    AgentTask,
    CriticVerdict,
    GraphMetrics,
    GraphResult,
    Handoff,
)
from copilot.graph.critic import Critic, build_critic
from copilot.graph.evidence_retriever import EvidenceRetriever, build_evidence_retriever
from copilot.graph.factory import build_graph
from copilot.graph.intake_extractor import IntakeExtractor, build_intake_extractor
from copilot.graph.supervisor import AgentGraph, Supervisor, build_supervisor

__all__ = [
    "AgentGraph",
    "AgentTask",
    "Critic",
    "CriticVerdict",
    "EvidenceRetriever",
    "GraphMetrics",
    "GraphResult",
    "Handoff",
    "IntakeExtractor",
    "Supervisor",
    "build_critic",
    "build_evidence_retriever",
    "build_graph",
    "build_intake_extractor",
    "build_supervisor",
]
