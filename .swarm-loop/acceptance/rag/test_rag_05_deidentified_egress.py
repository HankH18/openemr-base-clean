"""feat_rag criterion 5 — de-identified egress via the deidentify() choke point.

FROZEN GOALS. (a) ``deidentify()`` scrubs planted identifiers (labeled name,
MRN, SSN, DOB, phone) from a query while preserving the clinical topic.
(b) End-to-end: a retrieval run whose query carries the planted identifiers
must never leak any of them into the payloads sent to the embedder or the
reranker (captured by recording doubles injected through build_retriever).
"""

from __future__ import annotations

import _rag_helpers as H


async def test_rag_05_deidentify_scrubs_and_outbound_payloads_stay_clean():
    # (a) The scrub choke point itself.
    deidentify = H.resolve_deidentify()
    scrubbed = deidentify(H.PHI_QUERY)
    assert isinstance(scrubbed, str) and scrubbed.strip(), (
        "deidentify() must return a non-empty scrubbed query string"
    )
    H.assert_no_phi(scrubbed, "deidentify() output")
    low = scrubbed.lower()
    assert "ketoacidosis" in low and "insulin" in low, (
        "deidentify() must preserve the clinical topic (ketoacidosis / insulin) — "
        f"got {scrubbed!r}"
    )

    # (b) Stub-captured outbound payloads from a real retrieve() run.
    rec_embedder = H.RecordingEmbedder()
    rec_reranker = H.RecordingReranker()
    H.seed_corpus(lambda text: H.det_vector(text))  # public guideline text — not PHI
    calls_before = len(rec_embedder.calls)

    retriever = H.build_retriever(embedder=rec_embedder, reranker=rec_reranker)
    await H.retrieve(retriever, H.PHI_QUERY, top_k=4)

    outbound_embed = [t for call in rec_embedder.calls[calls_before:] for t in call]
    assert outbound_embed, (
        "the retriever must embed the query at retrieve() time through the injected "
        "embedder — nothing was captured"
    )
    for text in outbound_embed:
        H.assert_no_phi(text, "an embedder (Voyage) payload")
    for text in rec_reranker.captured_texts:
        H.assert_no_phi(text, "a reranker (Cohere) payload")

    # The de-identified clinical topic (not the identifiers) is what goes out.
    assert any("ketoacidosis" in t.lower() or "insulin" in t.lower() for t in outbound_embed), (
        f"the outbound query lost its clinical topic entirely: {outbound_embed!r}"
    )
