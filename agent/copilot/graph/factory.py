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

from collections.abc import Callable

from copilot.config import Settings
from copilot.fhir.client import FhirClient
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
    fhir_client_factory: Callable[[], FhirClient] | None = None,
) -> AgentGraph:
    """Assemble the graph; keyless settings select the full deterministic stub.

    ``fhir_client_factory`` lets a caller (the chat service in smart mode) inject
    the physician's delegated per-session reader; when ``None`` the graph falls
    back to the system-token client, exactly as the inline chat path does.
    """
    obs = observability if observability is not None else build_observability(settings)
    return AgentGraph(
        settings=settings,
        supervisor=build_supervisor(settings),
        intake_extractor=build_intake_extractor(settings),
        evidence_retriever=build_evidence_retriever(settings),
        critic=build_critic(settings),
        observability=obs,
        max_iterations=max_iterations,
        fhir_client_factory=fhir_client_factory,
    )
