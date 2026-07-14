"""Build the multi-agent graph from settings.

``build_graph`` wires the deterministic router, the two workers, and the critic
(each a Stub/Real dual chosen on API-key presence) into an
:class:`~copilot.graph.supervisor.AgentGraph`. Keyless settings yield the full
deterministic stub graph, so the whole path runs with no API key.

``observability`` defaults to the settings-appropriate backend (Noop when
Langfuse is not wired); ``max_iterations`` caps supervisor routing decisions
(one worker dispatch = one iteration).
"""

from __future__ import annotations

from copilot.config import Settings
from copilot.graph.critic import build_critic
from copilot.graph.evidence_retriever import build_evidence_retriever
from copilot.graph.intake_extractor import build_intake_extractor
from copilot.graph.supervisor import AgentGraph, build_supervisor
from copilot.observability import Observability, build_observability


def build_graph(
    settings: Settings,
    *,
    observability: Observability | None = None,
    max_iterations: int | None = None,
) -> AgentGraph:
    """Assemble the graph; keyless settings select the full deterministic stub."""
    obs = observability if observability is not None else build_observability(settings)
    return AgentGraph(
        settings=settings,
        supervisor=build_supervisor(settings),
        intake_extractor=build_intake_extractor(settings),
        evidence_retriever=build_evidence_retriever(settings),
        critic=build_critic(settings),
        observability=obs,
        max_iterations=max_iterations,
    )
