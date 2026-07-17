"""feat_rag criterion 4 — rerank via the Cohere Stub + fused-order fallback.

FROZEN GOALS. (a) The keyless factory yields a deterministic Stub reranker that
actually reorders by relevance: given one candidate that plainly matches the
query terms and two that do not, the matching candidate must come first, and
two identical calls must agree. (b) Reranker absence or failure never errors
the retrieval: a retriever whose injected reranker raises must return the same
(fused) ordering as one whose injected reranker is an identity pass-through,
and the fallback must be logged.
"""

from __future__ import annotations

import logging

import _rag_helpers as H

QUERY = "insulin infusion for diabetic ketoacidosis"
DOC_IRRELEVANT = "Colonoscopy screening intervals for average-risk adults."
DOC_MILD = "Hyperglycemia management in hospitalized patients."
DOC_MATCH = (
    "Begin an intravenous insulin infusion for diabetic ketoacidosis and monitor potassium."
)


async def test_rag_04_stub_rerank_reorders_and_failure_falls_back_to_fused(caplog):
    # (a) Stub reranker: deterministic, relevance-sensitive reordering.
    reranker = H.build_reranker()
    if reranker is None:
        H.fail(
            "build_reranker(settings) returned None with no cohere key — the pinned "
            "surface is a deterministic Stub reranker for keyless test runs"
        )
    docs = [DOC_IRRELEVANT, DOC_MILD, DOC_MATCH]
    ordered = await H.rerank_docs(reranker, QUERY, docs)
    ordered_again = await H.rerank_docs(reranker, QUERY, docs)
    assert sorted(ordered) == sorted(docs), (
        f"rerank must return a permutation of its candidates, got {ordered}"
    )
    assert ordered == ordered_again, "stub rerank must be deterministic across calls"
    assert ordered[0] == DOC_MATCH, (
        "the stub reranker must rank the candidate that matches the query terms first; "
        f"got {ordered[0]!r}"
    )
    assert ordered != docs, (
        "the stub reranker must actually reorder (input order deliberately buries the match)"
    )

    # (b) Fallback: a failing reranker never errors retrieval and preserves fused order.
    ids = H.seed_corpus(lambda text: H.det_vector(text))
    identity = H.RecordingReranker()  # identity ordering == the fused ordering
    retriever_fused = H.build_retriever(embedder=H.RecordingEmbedder(), reranker=identity)
    fused = H.evidence_items(await H.retrieve(retriever_fused, QUERY, top_k=len(ids)))
    fused_ids = [str(H.item_get(i, "chunk_id", "field_or_chunk_id", "id")) for i in fused]
    assert fused, "hybrid retrieval over the seeded corpus must return evidence"

    caplog.set_level(logging.DEBUG)
    retriever_boom = H.build_retriever(embedder=H.RecordingEmbedder(), reranker=H.BoomReranker())
    try:
        broken = H.evidence_items(await H.retrieve(retriever_boom, QUERY, top_k=len(ids)))
    except Exception as exc:  # noqa: BLE001 — the criterion: fallback, never an error
        H.fail(f"a reranker failure must fall back to fused order, not raise: {exc!r}")
    broken_ids = [str(H.item_get(i, "chunk_id", "field_or_chunk_id", "id")) for i in broken]
    assert broken_ids == fused_ids, (
        "on reranker failure the retriever must serve the fused sparse+dense ordering "
        f"unchanged: got {broken_ids}, fused {fused_ids}"
    )
    assert any("rerank" in rec.getMessage().lower() for rec in caplog.records), (
        "the rerank fallback must be logged (no silent degradation)"
    )

    # Absence (reranker=None) must also never error.
    retriever_absent = H.build_retriever(embedder=H.RecordingEmbedder(), reranker=None)
    try:
        await H.retrieve(retriever_absent, QUERY, top_k=len(ids))
    except Exception as exc:  # noqa: BLE001
        H.fail(f"retrieval with no reranker configured must not raise: {exc!r}")
