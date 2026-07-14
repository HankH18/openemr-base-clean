"""Evidence-retriever worker — the graph's guideline-RAG node.

Wraps the F6 hybrid retriever (:func:`copilot.rag.build_retriever` →
:class:`~copilot.rag.GuidelineRetriever`): given an
:class:`~copilot.graph.contracts.AgentTask`, it retrieves the top guideline
chunks for the question and reports the hit count + the typed
:class:`~copilot.rag.GuidelineEvidence`. An empty corpus yields zero hits (the
retriever returns ``[]``), which is honest no-evidence rather than a fabricated
citation.

Stub/Real sit behind the :class:`EvidenceRetriever` Protocol;
``build_evidence_retriever`` selects on API-key presence. Both wrap a
``GuidelineRetriever`` whose embedder/reranker are themselves keyless stubs when
no key is set, so the whole path runs deterministically offline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from copilot.config import Settings
from copilot.graph.contracts import AgentTask
from copilot.rag import GuidelineEvidence, GuidelineRetriever, build_retriever

_DEFAULT_TOP_K = 4


@dataclass(frozen=True)
class EvidenceReport:
    """What the evidence-retriever produced for one task."""

    hits: int
    evidence: list[GuidelineEvidence] = field(default_factory=list)


class EvidenceRetriever(Protocol):
    """The swappable evidence-retriever surface (Stub/Real behind this Protocol)."""

    async def run(self, task: AgentTask) -> EvidenceReport: ...


class StubEvidenceRetriever:
    """Deterministic, keyless evidence-retriever.

    Retrieves against the guideline corpus using the question verbatim; the
    underlying embedder/reranker are the keyless stubs.
    """

    def __init__(self, retriever: GuidelineRetriever, *, top_k: int = _DEFAULT_TOP_K) -> None:
        self._retriever = retriever
        self._top_k = top_k

    async def run(self, task: AgentTask) -> EvidenceReport:
        evidence = await self._retriever.retrieve(task.question, top_k=self._top_k)
        return EvidenceReport(hits=len(evidence), evidence=list(evidence))


class RealEvidenceRetriever:
    """Keyed evidence-retriever — wraps the real (Voyage/Cohere) retriever.

    Uses the question as the retrieval query today; the keyed path is where a
    future LLM query-reformulation would live. Behaviour otherwise mirrors the
    Stub so the evidence contract is identical.
    """

    def __init__(
        self, settings: Settings, retriever: GuidelineRetriever, *, top_k: int = _DEFAULT_TOP_K
    ) -> None:
        self._settings = settings
        self._retriever = retriever
        self._top_k = top_k

    async def run(self, task: AgentTask) -> EvidenceReport:
        evidence = await self._retriever.retrieve(task.question, top_k=self._top_k)
        return EvidenceReport(hits=len(evidence), evidence=list(evidence))


def build_evidence_retriever(
    settings: Settings, *, retriever: GuidelineRetriever | None = None
) -> EvidenceRetriever:
    """Keyless settings → the Stub; a key → the Real retriever.

    ``retriever`` is an injection point (tests/DI); ``None`` builds the
    settings-appropriate hybrid retriever (keyless stubs when no key).
    """
    resolved = retriever if retriever is not None else build_retriever(settings)
    if not settings.anthropic_api_key:
        return StubEvidenceRetriever(resolved)
    return RealEvidenceRetriever(settings, resolved)
