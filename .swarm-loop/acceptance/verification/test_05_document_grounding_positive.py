"""feat_verification criterion 5 — document grounding, positive path.

A document-cited claim verifies when the claimed value re-checks against the
STORED schema-validated extracted_fact (agent-store authoritative — no FHIR
fetch involved), the fact is supported=True, and its reconciled bbox/confidence
is at/above threshold. FROZEN GOAL HARNESS.
"""

from __future__ import annotations

from ._helpers import (
    FailingReader,
    action_of,
    make_claim,
    make_document_citation,
    passing_claims,
    run_verify,
    seed_document_fact,
)


async def test_05_document_grounding_positive(db_path):
    doc_id, _ext_id, fact_id = seed_document_fact(
        db_path,
        patient_id=1001,
        value="13.5",
        field_path="hemoglobin",
        unit="g/dL",
        page_no=1,
        bbox=[0.10, 0.20, 0.25, 0.04],
        match_confidence=0.95,
        supported=True,
    )
    citation = make_document_citation(
        source_id=doc_id,
        page=1,
        fact_id=fact_id,
        value="13.5",
        bbox=[0.10, 0.20, 0.25, 0.04],
        confidence=0.95,
    )
    claim = make_claim("Outside lab reports hemoglobin 13.5 g/dL", citation)

    # FailingReader: document grounding must be satisfied by the agent store
    # alone (labs are not FHIR-writable — the store is authoritative).
    result = await run_verify([claim], 1001, FailingReader())

    assert action_of(result) == "served", (
        f"a stored, supported, above-threshold fact must ground the claim "
        f"(got action={action_of(result)!r})"
    )
    passing = passing_claims(result)
    assert len(passing) == 1 and passing[0].text == "Outside lab reports hemoglobin 13.5 g/dL"
    assert result.passed is True
