"""feat_verification criterion 4 — interim fail-closed for non-fhir citations.

With the union in place, fhir claims verify exactly as before, and a document
or guideline citation that cannot be grounded (here: nothing in the agent
store backs it) is treated UNVERIFIABLE and dropped — no crash, never a false
verify. This holds in the F1-only interim AND after F5 lands (an unbacked
citation stays unverifiable). FROZEN GOAL HARNESS.
"""

from __future__ import annotations

from ._helpers import (
    MappingReader,
    action_of,
    make_claim,
    make_document_citation,
    make_guideline_citation,
    passing_claims,
    run_verify,
)


async def test_04_interim_fail_closed_non_fhir_dropped(db_path):
    from copilot.domain.primitives import FhirReference, ResourceType

    obs = {
        "resourceType": "Observation",
        "id": "obs-hgb",
        "status": "final",
        "code": {"text": "Hemoglobin"},
        "valueQuantity": {"value": 13.5, "unit": "g/dL"},
    }
    reader = MappingReader([obs])

    fhir_claim = make_claim(
        "Hemoglobin 13.5 g/dL",
        FhirReference(
            resource_type=ResourceType.Observation,
            resource_id="obs-hgb",
            field="valueQuantity.value",
            value="13.5",
        ),
    )
    # Nothing is seeded in the agent store: both citations below are unbacked.
    doc_claim = make_claim(
        "Outside lab reports sodium 140",
        make_document_citation(
            source_id=999901, page=1, fact_id=999902, value="140",
            bbox=[0.1, 0.1, 0.1, 0.03], confidence=0.9,
        ),
    )
    gl_claim = make_claim(
        "The guideline advises early fluid resuscitation",
        make_guideline_citation(
            source_id=999903, section="sepsis", chunk_id=999904,
            quote="early fluid resuscitation",
        ),
    )

    result = await run_verify([fhir_claim, doc_claim, gl_claim], 1001, reader)

    assert action_of(result) == "degraded", (
        "fhir claim must still verify; unbacked non-fhir claims must be dropped "
        f"(got action={action_of(result)!r})"
    )
    passing = passing_claims(result)
    assert [c.text for c in passing] == ["Hemoglobin 13.5 g/dL"], (
        "exactly the fhir claim survives; document/guideline citations with no "
        "backing store rows are unverifiable"
    )
    assert len(result.claims) == 3, (
        "dropped non-fhir claims must be REPORTED as failed per-claim results, not crash"
    )
    assert result.passed is False
