"""Guideline RAG (Week 2) — hospitalist corpus ingest + embeddings.

This package currently carries the F6-corpus slice: the :class:`Embedder`
Protocol with its deterministic keyless Voyage Stub (``build_embedder``) and
the idempotent corpus ingest (``ingest_corpus`` — see
``agent/scripts/ingest_guidelines.py`` for the pinned CLI entry point). The
hybrid retriever / RRF fusion / rerank surface lands with F6-retriever (C2).
"""

from copilot.rag.embeddings import Embedder, StubEmbedder, build_embedder
from copilot.rag.ingest import (
    CorpusChunk,
    CorpusDocument,
    IngestReport,
    discover_corpus,
    ingest_corpus,
)

__all__ = [
    "CorpusChunk",
    "CorpusDocument",
    "Embedder",
    "IngestReport",
    "StubEmbedder",
    "build_embedder",
    "discover_corpus",
    "ingest_corpus",
]
