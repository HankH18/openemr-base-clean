"""Evidence-retriever worker — the graph's guideline-RAG node.

Wraps the F6 hybrid retriever (:func:`copilot.rag.build_retriever` →
:class:`~copilot.rag.GuidelineRetriever`): given an
:class:`~copilot.graph.contracts.AgentTask`, it retrieves the top guideline
chunks for the question and reports the hit count + the typed
:class:`~copilot.rag.GuidelineEvidence`. An empty corpus yields zero hits (the
retriever returns ``[]``), which is honest no-evidence rather than a fabricated
citation.

There is ONE worker class behind the :class:`EvidenceRetriever` Protocol — the
keyed vs keyless distinction lives entirely in the wrapped ``GuidelineRetriever``
(its embedder/reranker are the real Voyage/Cohere clients when keyed and
deterministic keyless stubs otherwise, selected inside ``build_retriever``). This
wrapper's behaviour is identical either way, so there is nothing for a Real/Stub
split at this layer to differentiate. ``build_evidence_retriever`` builds the
worker over the settings-appropriate retriever; the whole path runs
deterministically offline when no key is set.
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
    """The evidence-retriever surface (one implementation behind this Protocol)."""

    async def run(self, task: AgentTask) -> EvidenceReport: ...


class GuidelineEvidenceRetriever:
    """Retrieves guideline evidence via the wrapped ``GuidelineRetriever``.

    Uses the question verbatim as the retrieval query. All keyed vs keyless
    behaviour lives in the wrapped retriever (real embedder/reranker when keyed,
    deterministic stubs when not), so this wrapper is provider-agnostic.
    """

    def __init__(self, retriever: GuidelineRetriever, *, top_k: int = _DEFAULT_TOP_K) -> None:
        self._retriever = retriever
        self._top_k = top_k

    async def run(self, task: AgentTask) -> EvidenceReport:
        evidence = await self._retriever.retrieve(task.question, top_k=self._top_k)
        return EvidenceReport(hits=len(evidence), evidence=list(evidence))


def build_evidence_retriever(
    settings: Settings, *, retriever: GuidelineRetriever | None = None
) -> EvidenceRetriever:
    """Build the evidence-retriever over the settings-appropriate retriever.

    ``retriever`` is an injection point (tests/DI); ``None`` builds the
    settings-appropriate hybrid retriever (keyless stubs when no key), so the
    keyed/keyless distinction is resolved there, not by a wrapper split.
    """
    resolved = retriever if retriever is not None else build_retriever(settings)
    return GuidelineEvidenceRetriever(resolved)
