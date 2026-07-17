"""feat_ingestion criterion 6 — pipeline persistence + audit + mid-failure.

`attach_and_extract` persists source_document + document_page + extraction +
extracted_fact with correlation ids and an audit trail, ending status=
'extracted'; a mid-pipeline failure (post-upload, unreadable bytes) fails
closed: status='failed', zero extractions, zero orphan facts.
FROZEN GOAL HARNESS.
"""

from __future__ import annotations

from ._helpers import audit_entries, build_fixture_pdf, fetch_rows, resolve_attach


async def test_06_pipeline_persistence_and_audit(db_path, tmp_path, settings, fake_openemr):
    from copilot.memory.models import (
        DocumentPageRow,
        ExtractedFactRow,
        ExtractionRow,
        SourceDocumentRow,
    )

    attach = resolve_attach(settings, tmp_path)
    pdf = build_fixture_pdf(("Hemoglobin 13.5 g/dL", "Potassium 4.2 mmol/L"))

    await attach(patient_pid=1001, content=pdf, doc_type="lab_pdf", filename="pipeline.pdf")

    docs = fetch_rows(db_path, SourceDocumentRow, patient_id=1001)
    assert len(docs) == 1, f"expected exactly one source_document (got {len(docs)})"
    doc = docs[0]
    assert doc.status == "extracted", f"final status must be 'extracted' (got {doc.status!r})"
    assert doc.correlation_id, "source_document must carry a correlation_id"
    assert doc.openemr_document_id, "the OpenEMR upload must be recorded on the row"

    pages = fetch_rows(db_path, DocumentPageRow, source_document_id=doc.id)
    assert pages, "page renders must be persisted"
    assert all((p.width or 0) > 0 and (p.height or 0) > 0 for p in pages)

    exts = fetch_rows(db_path, ExtractionRow, source_document_id=doc.id)
    assert exts, "the extraction run must be persisted"
    assert all(e.correlation_id for e in exts), "extraction rows carry correlation ids"

    facts = [f for e in exts for f in fetch_rows(db_path, ExtractedFactRow, extraction_id=e.id)]
    assert facts, "extracted facts must be persisted"

    audits = [
        r
        for r in audit_entries(db_path, patient_id=1001)
        if any(k in (r.action or "") for k in ("doc", "ingest", "extract"))
    ]
    assert audits, (
        "ingestion must leave an audit trail (document.ingest / extraction.run rows)"
    )

    # --- mid-pipeline failure: upload succeeds, rasterization cannot ---
    try:
        await attach(
            patient_pid=1002,
            content=b"this is not a pdf at all",
            doc_type="lab_pdf",
            filename="broken.bin",
        )
    except Exception:
        pass  # raising is acceptable — the DB contract below is what is frozen

    broken = fetch_rows(db_path, SourceDocumentRow, patient_id=1002)
    assert broken, (
        "a failed ingestion must still record the attempt (status='failed') so the "
        "status endpoint can surface it"
    )
    assert all(d.status == "failed" for d in broken), (
        f"mid-failure must end status='failed' (got {[d.status for d in broken]})"
    )
    bad_exts = [
        e for d in broken for e in fetch_rows(db_path, ExtractionRow, source_document_id=d.id)
    ]
    assert not bad_exts, "no extraction row may persist for a failed ingestion"
    assert len(fetch_rows(db_path, ExtractedFactRow)) == len(facts), (
        "zero orphan facts: the failed ingestion must not add extracted_fact rows"
    )
