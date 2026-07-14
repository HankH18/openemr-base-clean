"""feat_verification criterion 6 — document grounding, negative paths.

Each of: claimed value mismatching the stored fact / citation pointing at a
missing extraction / fact persisted supported=False / reconciliation
confidence below any sane threshold (0.05) → the claim is DROPPED fail-closed
(single-claim answer collapses to withheld). FROZEN GOAL HARNESS.
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


async def test_06_document_grounding_negatives(db_path):
    bbox = [0.10, 0.20, 0.25, 0.04]

    # (a) value mismatch: store says 13.5, the claim asserts 14.9.
    d1, _e1, f1 = seed_document_fact(db_path, patient_id=1001, value="13.5")
    mismatch = make_claim(
        "Outside lab reports hemoglobin 14.9 g/dL",
        make_document_citation(
            source_id=d1, page=1, fact_id=f1, value="14.9", bbox=bbox, confidence=0.95
        ),
    )

    # (b) missing extraction: citation points at rows that do not exist.
    missing = make_claim(
        "Outside lab reports sodium 140",
        make_document_citation(
            source_id=778899, page=1, fact_id=778900, value="140", bbox=bbox, confidence=0.95
        ),
    )

    # (c) stored fact was never located on the page (supported=False).
    d3, _e3, f3 = seed_document_fact(
        db_path, patient_id=1001, value="9.9", field_path="lactate", unit="mmol/L",
        supported=False, match_confidence=None, bbox=None,
    )
    unsupported = make_claim(
        "Outside lab reports lactate 9.9",
        make_document_citation(
            source_id=d3, page=1, fact_id=f3, value="9.9",
            bbox=[0.1, 0.1, 0.1, 0.03], confidence=0.9,
        ),
    )

    # (d) reconciled below any sane confidence threshold (frozen floor: a fact
    # at 0.05 must never ground a claim).
    d4, _e4, f4 = seed_document_fact(
        db_path, patient_id=1001, value="7.7", field_path="glucose", unit="mmol/L",
        supported=True, match_confidence=0.05,
    )
    below = make_claim(
        "Outside lab reports glucose 7.7",
        make_document_citation(
            source_id=d4, page=1, fact_id=f4, value="7.7", bbox=bbox, confidence=0.05
        ),
    )

    for label, claim in (
        ("value mismatch", mismatch),
        ("missing extraction", missing),
        ("supported=False", unsupported),
        ("below-threshold confidence", below),
    ):
        result = await run_verify([claim], 1001, FailingReader())
        assert action_of(result) == "withheld", (
            f"{label}: the claim must be dropped fail-closed "
            f"(got action={action_of(result)!r})"
        )
        assert not passing_claims(result), f"{label}: no claim may pass"
        assert result.passed is False, label
