"""Guideline RAG (Week 2) — hospitalist corpus ingest + hybrid retrieval.

This package carries the guideline-RAG slice:

- **F6-corpus**: the :class:`Embedder` Protocol with its deterministic keyless
  Voyage Stub (``build_embedder``) and the idempotent corpus ingest
  (``ingest_corpus`` — see ``agent/scripts/ingest_guidelines.py`` for the CLI).
- **F6-retriever**: the hybrid sparse+dense retriever fused with Reciprocal
  Rank Fusion (``build_retriever`` / ``rrf_fuse``), the Cohere rerank behind a
  Stub/Real Protocol (``build_reranker``) with a fused-order fallback, the
  ``deidentify`` PHI-scrub query choke point, and the typed
  :class:`GuidelineEvidence` contract.
"""

from copilot.rag.deidentify import deidentify
from copilot.rag.embeddings import Embedder, StubEmbedder, VoyageEmbedder, build_embedder
from copilot.rag.ingest import (
    CorpusChunk,
    CorpusDocument,
    IngestReport,
    discover_corpus,
    ingest_corpus,
)
from copilot.rag.rerank import CohereReranker, Reranker, StubReranker, build_reranker
from copilot.rag.retriever import (
    GuidelineEvidence,
    GuidelineRetriever,
    build_retriever,
    rrf_fuse,
    rrf_scores,
)

__all__ = [
    "CohereReranker",
    "CorpusChunk",
    "CorpusDocument",
    "Embedder",
    "GuidelineEvidence",
    "GuidelineRetriever",
    "IngestReport",
    "Reranker",
    "StubEmbedder",
    "StubReranker",
    "VoyageEmbedder",
    "build_embedder",
    "build_reranker",
    "build_retriever",
    "deidentify",
    "discover_corpus",
    "ingest_corpus",
    "rrf_fuse",
    "rrf_scores",
]
