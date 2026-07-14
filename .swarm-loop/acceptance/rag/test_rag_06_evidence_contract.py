"""feat_rag criterion 6 — typed guideline-evidence contract + explicit no-evidence.

FROZEN GOALS. Top-K results carry ``chunk_id`` + ``section`` and are typed as
guideline evidence (source_type "guideline" / a Guideline* type), never as
patient-fact Claims; every returned chunk_id must exist in the corpus (no
fabricated citations). An empty-corpus retrieval returns an explicit
no-evidence result — empty, typed, and without inventing a citation — rather
than raising.
"""

from __future__ import annotations

import importlib

import _rag_helpers as H


def _is_guideline_typed(item) -> bool:
    source_type = H.item_get(item, "source_type")
    if source_type is not None and str(source_type) == "guideline":
        return True
    if "guideline" in type(item).__name__.lower():
        return True
    citation = getattr(item, "citation", None)
    if citation is not None and "guideline" in type(citation).__name__.lower():
        return True
    return False


async def test_rag_06_typed_guideline_evidence_and_explicit_no_evidence():
    # Empty corpus FIRST: explicit no-evidence, no fabricated citation, no crash.
    empty_retriever = H.build_retriever()
    try:
        empty_result = await H.retrieve(
            empty_retriever, "guideline recommendations for diabetic ketoacidosis", top_k=4
        )
    except Exception as exc:  # noqa: BLE001 — empty retrieval is a normal outcome
        H.fail(f"empty retrieval must be an explicit no-evidence result, not an error: {exc!r}")
    empty_items = H.evidence_items(empty_result)
    assert empty_items == [], (
        f"an empty corpus must yield zero evidence items (no fabrication), got {empty_items!r}"
    )

    # Seeded corpus: the typed evidence contract.
    ids = H.seed_corpus(lambda text: H.det_vector(text))
    retriever = H.build_retriever()
    result = await H.retrieve(
        retriever, "intravenous insulin infusion for diabetic ketoacidosis", top_k=len(ids)
    )
    items = H.evidence_items(result)
    assert items, "retrieval over the seeded corpus must return evidence items"

    from copilot.domain.contracts import Claim

    for item in items:
        chunk_id = H.item_get(item, "chunk_id", "field_or_chunk_id")
        section = H.item_get(item, "section", "page_or_section")
        assert chunk_id is not None, f"evidence item must carry chunk_id: {item!r}"
        assert section is not None and str(section).strip(), (
            f"evidence item must carry its section: {item!r}"
        )
        assert str(chunk_id) in ids, (
            f"evidence cites chunk_id {chunk_id!r} which is not in the corpus "
            f"(known ids {sorted(ids)}) — citations must never be fabricated"
        )
        assert str(ids[str(chunk_id)]["section"]) == str(section), (
            f"evidence section {section!r} does not match the stored chunk's section "
            f"{ids[str(chunk_id)]['section']!r}"
        )
        assert not isinstance(item, Claim), (
            "guideline evidence must not be typed as a patient-fact Claim"
        )
        assert _is_guideline_typed(item), (
            "evidence items must be explicitly typed as guideline evidence "
            f"(source_type='guideline' or a Guideline* model): {item!r}"
        )

    # If the Week-2 citation union has landed, guideline evidence must be
    # expressible as GuidelineCitation — never as the FHIR/patient variants.
    primitives = importlib.import_module("copilot.domain.primitives")
    guideline_citation = getattr(primitives, "GuidelineCitation", None)
    if guideline_citation is not None:
        for item in items:
            citation = getattr(item, "citation", None)
            if citation is not None:
                assert isinstance(citation, guideline_citation), (
                    f"an evidence citation must be a GuidelineCitation, got {type(citation)}"
                )
