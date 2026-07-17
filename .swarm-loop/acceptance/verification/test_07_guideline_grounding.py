"""feat_verification criterion 7 — guideline grounding, positive + negative.

A guideline-cited claim verifies iff the quoted text appears verbatim in the
STORED guideline chunk; an absent quote or a missing chunk drops the claim
fail-closed. FROZEN GOAL HARNESS.
"""

from __future__ import annotations

from ._helpers import (
    FailingReader,
    action_of,
    make_claim,
    make_guideline_citation,
    passing_claims,
    run_verify,
    seed_guideline_chunk,
)

_CONTENT = (
    "In diabetic ketoacidosis, begin an insulin infusion only after initial "
    "fluid resuscitation and confirm the serum potassium is at least 3.3 mmol "
    "per liter before starting."
)


async def test_07_guideline_grounding(db_path):
    gdoc_id, chunk_id = seed_guideline_chunk(
        db_path, content=_CONTENT, section="dka-treatment"
    )

    # Positive: the quote appears verbatim in the stored chunk.
    quote = "confirm the serum potassium is at least 3.3 mmol per liter"
    good = make_claim(
        "The guideline requires potassium of at least 3.3 before insulin is started",
        make_guideline_citation(
            source_id=gdoc_id, section="dka-treatment", chunk_id=chunk_id, quote=quote
        ),
    )
    result = await run_verify([good], 1001, FailingReader())
    assert action_of(result) == "served", (
        f"a verbatim quote-in-chunk must verify (got action={action_of(result)!r})"
    )
    assert len(passing_claims(result)) == 1

    # Negative: the quoted text does NOT appear in the chunk.
    absent = make_claim(
        "The guideline advises an immediate insulin bolus",
        make_guideline_citation(
            source_id=gdoc_id, section="dka-treatment", chunk_id=chunk_id,
            quote="give an immediate insulin bolus",
        ),
    )
    r2 = await run_verify([absent], 1001, FailingReader())
    assert action_of(r2) == "withheld", "an absent quote must drop the claim fail-closed"
    assert not passing_claims(r2)

    # Negative: the cited chunk does not exist.
    missing = make_claim(
        "The guideline requires potassium monitoring",
        make_guideline_citation(
            source_id=gdoc_id, section="dka-treatment", chunk_id=424242, quote=quote
        ),
    )
    r3 = await run_verify([missing], 1001, FailingReader())
    assert action_of(r3) == "withheld", "a missing chunk must drop the claim fail-closed"
    assert not passing_claims(r3)
