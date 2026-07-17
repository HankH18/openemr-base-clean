"""feat_verification criterion 1 — citation-union round-trip + Week-1 back-compat.

The `Claim.source_ref` discriminated union (FhirCitation | DocumentCitation |
GuidelineCitation, discriminated on `source_type`): Week-1 persisted claims
(no `source_type`) rehydrate as fhir citations unchanged, and a summary carrying
all three citation kinds round-trips byte-equal through the repository
serializers. FROZEN GOAL HARNESS — do not edit to make it pass.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from ._helpers import (
    fetch_rows,
    field,
    insert_rows,
    make_claim,
    make_document_citation,
    make_guideline_citation,
)

_NOW = dt.datetime(2026, 7, 1, 12, 0, 0)


def _summary_json(db_path, patient_id: int) -> str:
    from copilot.memory.models import MemoryFileRow

    rows = fetch_rows(db_path, MemoryFileRow, patient_id=patient_id)
    assert rows, f"no memory_file row for patient {patient_id}"
    return json.dumps(rows[0].summary, sort_keys=True)


def _source_type(ref) -> str:
    st = getattr(ref, "source_type", None)
    if st is None:
        pytest.fail(
            "rehydrated claim's source_ref has no source_type — the citation union "
            "(back-compat default 'fhir') is not implemented"
        )
    return str(getattr(st, "value", st))


async def test_01_citation_union_roundtrip(db_path):
    from copilot.domain.contracts import MemoryFileSummary
    from copilot.domain.primitives import FhirReference, PatientId, ResourceType
    from copilot.memory.db import get_session_factory
    from copilot.memory.models import MemoryFileRow
    from copilot.memory.repository import MemoryRepository

    # --- part A: a Week-1 persisted row (no source_type) rehydrates as fhir ---
    week1_claim = {
        "text": "Hemoglobin 13.5 g/dL",
        "severity": None,
        "trend_direction": None,
        "value_direction": None,
        "source_ref": {
            "resource_type": "Observation",
            "resource_id": "obs-w1",
            "field": "valueQuantity.value",
            "value": "13.5",
            "last_updated": None,
            "timestamp": None,
        },
    }
    insert_rows(
        db_path,
        MemoryFileRow(
            patient_id=4242,
            summary={"patient_id": 4242, "claims": [week1_claim], "changes": []},
            acuity_score=1.0,
            rank_reason="week1",
            synthesized_at=_NOW,
            source_watermark=_NOW,
            content_hash="w1hash",
        ),
    )

    async with get_session_factory()() as session:
        repo = MemoryRepository(session)
        loaded = await repo.get_memory_file(PatientId(value=4242))
        assert loaded is not None and loaded.claims, "Week-1 row failed to rehydrate"
        ref = loaded.claims[0].source_ref
        assert _source_type(ref) == "fhir", "legacy claims must default to source_type='fhir'"
        assert field(ref, "resource_id") == "obs-w1"
        assert field(ref, "value", "quote_or_value") == "13.5"

        # --- part B: all three citation kinds round-trip byte-equal ---
        doc_cit = make_document_citation(
            source_id=11, page=1, fact_id=21, value="13.5",
            bbox=[0.1, 0.2, 0.25, 0.04], confidence=0.95,
        )
        gl_cit = make_guideline_citation(
            source_id=31, section="dka-treatment", chunk_id=41,
            quote="begin an insulin infusion after fluid resuscitation",
        )
        fhir_ref = FhirReference(
            resource_type=ResourceType.Observation,
            resource_id="obs-union",
            field="valueQuantity.value",
            value="4.2",
        )
        claims = [
            make_claim("Outside lab: hemoglobin 13.5 g/dL", doc_cit),
            make_claim("Guideline: insulin infusion follows fluids", gl_cit),
            make_claim("Potassium 4.2 mmol/L", fhir_ref),
        ]
        summary = MemoryFileSummary(
            patient_id=PatientId(value=4243),
            claims=claims,
            changes=[],
            acuity_score=2.0,
            rank_reason="week2 union",
            synthesized_at=_NOW,
            source_watermark=_NOW,
            content_hash="w2hash",
        )
        await repo.save_memory_file(summary)
        await session.commit()

    j1 = _summary_json(db_path, 4243)

    async with get_session_factory()() as session:
        repo = MemoryRepository(session)
        loaded2 = await repo.get_memory_file(PatientId(value=4243))
        assert loaded2 is not None and len(loaded2.claims) == 3
        kinds = [_source_type(c.source_ref) for c in loaded2.claims]
        assert kinds == ["document", "guideline", "fhir"], f"citation kinds lost: {kinds}"
        doc_back = loaded2.claims[0].source_ref
        assert field(doc_back, "quote_or_value") == "13.5"
        assert [float(v) for v in field(doc_back, "bbox")] == [0.1, 0.2, 0.25, 0.04]
        assert float(field(doc_back, "confidence")) == pytest.approx(0.95)
        gl_back = loaded2.claims[1].source_ref
        assert field(gl_back, "quote_or_value") == (
            "begin an insulin infusion after fluid resuscitation"
        )
        # Re-save what was loaded: serializers must be a byte-equal round-trip.
        await repo.save_memory_file(loaded2)
        await session.commit()

    j2 = _summary_json(db_path, 4243)
    assert j1 == j2, "repository (de)serializers are not a byte-equal round-trip"
