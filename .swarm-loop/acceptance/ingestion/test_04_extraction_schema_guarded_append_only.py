"""feat_ingestion criterion 4 — schema-guarded extraction, append-only store.

The keyless stub-vision extraction persists only schema-validated facts; a
malformed field is rejected by the strict schema (never coerced); re-ingesting
the same document appends a NEW extraction row while every prior row stays
byte-identical. FROZEN GOAL HARNESS.
"""

from __future__ import annotations

import pytest

from ._helpers import (
    VALID_FACT_PAYLOAD,
    build_fixture_pdf,
    fetch_rows,
    resolve_attach,
    schema_class,
)


async def test_04_extraction_schema_guarded_append_only(db_path, tmp_path, settings):
    import pydantic

    from copilot.memory.models import ExtractedFactRow, ExtractionRow, SourceDocumentRow

    attach = resolve_attach(settings, tmp_path)
    pdf = build_fixture_pdf(("Hemoglobin 13.5 g/dL",))

    await attach(patient_pid=1001, content=pdf, doc_type="lab_pdf", filename="append.pdf")

    docs = fetch_rows(db_path, SourceDocumentRow, patient_id=1001)
    assert docs, "attach_and_extract persisted no source_document row"
    exts1 = [e for d in docs for e in fetch_rows(db_path, ExtractionRow, source_document_id=d.id)]
    assert exts1, "attach_and_extract persisted no extraction row"
    first_ids = {e.id for e in exts1}

    facts1 = [f for e in exts1 for f in fetch_rows(db_path, ExtractedFactRow, extraction_id=e.id)]
    assert facts1, "the stub-vision extraction persisted no schema-validated facts"
    assert all(f.field_path for f in facts1), "every persisted fact carries its field_path"
    snapshot = {
        (f.id, f.field_path, f.value, f.supported, f.match_confidence) for f in facts1
    }

    # Re-ingest the identical bytes: append-only — one NEW extraction row.
    await attach(patient_pid=1001, content=pdf, doc_type="lab_pdf", filename="append.pdf")

    docs2 = fetch_rows(db_path, SourceDocumentRow, patient_id=1001)
    exts2 = [e for d in docs2 for e in fetch_rows(db_path, ExtractionRow, source_document_id=d.id)]
    assert len(exts2) == len(exts1) + 1, (
        f"re-ingest must append exactly one NEW extraction row "
        f"(before={len(exts1)}, after={len(exts2)})"
    )
    assert first_ids < {e.id for e in exts2}, "prior extraction rows must survive re-ingest"

    facts_after = {
        (f.id, f.field_path, f.value, f.supported, f.match_confidence)
        for eid in first_ids
        for f in fetch_rows(db_path, ExtractedFactRow, extraction_id=eid)
    }
    assert facts_after == snapshot, "prior extractions' facts must be untouched by re-ingest"

    # A malformed field coming out of the (tool-forced JSON) extraction is
    # rejected by the strict schema — validation error, never coercion.
    extracted_fact = schema_class("ExtractedFact")
    with pytest.raises(pydantic.ValidationError):
        extracted_fact.model_validate({**VALID_FACT_PAYLOAD, "bbox": "not-a-box"})
    with pytest.raises(pydantic.ValidationError):
        extracted_fact.model_validate({**VALID_FACT_PAYLOAD, "match_confidence": "high"})
