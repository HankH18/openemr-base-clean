"""feat_verification criterion 8 — fail-closed end-to-end over mixed claims.

An answer mixing fhir + document + guideline claims keeps ONLY the verifiable
ones (degraded); an answer whose claims all fail collapses to withheld — the
Week-1 contract, intact across all three citation types. FROZEN GOAL HARNESS.
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
    seed_document_fact,
    seed_guideline_chunk,
)

_CONTENT = (
    "For sepsis with elevated lactate, give a 30 mL per kg crystalloid bolus "
    "and reassess perfusion within the first hour."
)


async def test_08_fail_closed_end_to_end(db_path):
    from copilot.domain.primitives import FhirReference, ResourceType

    obs = {
        "resourceType": "Observation",
        "id": "obs-k",
        "status": "final",
        "code": {"text": "Potassium"},
        "valueQuantity": {"value": 5.6, "unit": "mmol/L"},
    }
    reader = MappingReader([obs])

    doc_id, _ext, fact_id = seed_document_fact(
        db_path, patient_id=1002, value="13.5", field_path="hemoglobin"
    )
    gdoc_id, chunk_id = seed_guideline_chunk(db_path, content=_CONTENT, section="sepsis")

    good_fhir = make_claim(
        "Potassium 5.6 mmol/L",
        FhirReference(
            resource_type=ResourceType.Observation,
            resource_id="obs-k",
            field="valueQuantity.value",
            value="5.6",
        ),
    )
    good_doc = make_claim(
        "Outside lab reports hemoglobin 13.5 g/dL",
        make_document_citation(
            source_id=doc_id, page=1, fact_id=fact_id, value="13.5",
            bbox=[0.10, 0.20, 0.25, 0.04], confidence=0.95,
        ),
    )
    good_gl = make_claim(
        "The guideline calls for a 30 mL per kg crystalloid bolus",
        make_guideline_citation(
            source_id=gdoc_id, section="sepsis", chunk_id=chunk_id,
            quote="give a 30 mL per kg crystalloid bolus",
        ),
    )
    bad_doc = make_claim(
        "Outside lab reports hemoglobin 14.9 g/dL",
        make_document_citation(
            source_id=doc_id, page=1, fact_id=fact_id, value="14.9",
            bbox=[0.10, 0.20, 0.25, 0.04], confidence=0.95,
        ),
    )

    result = await run_verify([good_fhir, good_doc, good_gl, bad_doc], 1002, reader)
    assert action_of(result) == "degraded", (
        f"mixed answer: verifiable claims survive, the rest drop "
        f"(got action={action_of(result)!r})"
    )
    surviving = {c.text for c in passing_claims(result)}
    assert surviving == {
        "Potassium 5.6 mmol/L",
        "Outside lab reports hemoglobin 13.5 g/dL",
        "The guideline calls for a 30 mL per kg crystalloid bolus",
    }, f"unexpected survivors: {surviving}"

    # Zero survivors -> withheld (the Week-1 contract, all citation types).
    bad_fhir = make_claim(
        "Creatinine 2.4 mg/dL",
        FhirReference(
            resource_type=ResourceType.Observation,
            resource_id="obs-does-not-exist",
            field="valueQuantity.value",
            value="2.4",
        ),
    )
    bad_doc2 = make_claim(
        "Outside lab reports sodium 128",
        make_document_citation(
            source_id=889900, page=1, fact_id=889901, value="128",
            bbox=[0.1, 0.1, 0.1, 0.03], confidence=0.9,
        ),
    )
    bad_gl = make_claim(
        "The guideline advises immediate vasopressors",
        make_guideline_citation(
            source_id=gdoc_id, section="sepsis", chunk_id=chunk_id,
            quote="start vasopressors before any fluids",
        ),
    )
    r2 = await run_verify([bad_fhir, bad_doc2, bad_gl], 1002, reader)
    assert action_of(r2) == "withheld", (
        f"zero verifiable claims must withhold the answer (got {action_of(r2)!r})"
    )
    assert not passing_claims(r2)
    assert r2.passed is False
