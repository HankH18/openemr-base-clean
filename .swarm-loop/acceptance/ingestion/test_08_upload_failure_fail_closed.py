"""feat_ingestion criterion 8 — upload failure fails the ingestion closed.

When OpenEMR rejects the source-document upload (500), ingestion fails closed:
the attempt is recorded status='failed' with no openemr_document_id, no
extraction, no facts — and the failure is surfaced (raised, or a failed status
result), never a silent success. FROZEN GOAL HARNESS.
"""

from __future__ import annotations

from ._helpers import build_fixture_pdf, fetch_rows, opt_field, resolve_attach


async def test_08_upload_failure_fails_closed(db_path, tmp_path, settings, fake_openemr):
    from copilot.memory.models import ExtractedFactRow, ExtractionRow, SourceDocumentRow

    attach = resolve_attach(settings, tmp_path)
    fake_openemr.DOCUMENT_UPLOAD_MODE = "error"  # force the fake's document route to 500

    pdf = build_fixture_pdf(("Lactate 5.0 mmol/L",))
    raised = False
    result = None
    try:
        result = await attach(
            patient_pid=1004, content=pdf, doc_type="lab_pdf", filename="failing.pdf"
        )
    except Exception:
        raised = True  # raising surfaces the error — acceptable

    rows = fetch_rows(db_path, SourceDocumentRow, patient_id=1004)
    assert rows, (
        "a failed upload must still record the attempt (status='failed') so the "
        "status endpoint can surface the error"
    )
    assert all(r.status == "failed" for r in rows), (
        f"ingestion must fail closed with status='failed' (got {[r.status for r in rows]})"
    )
    assert all(not r.openemr_document_id for r in rows), (
        "no openemr_document_id may be stored when the upload failed"
    )

    exts = [
        e for r in rows for e in fetch_rows(db_path, ExtractionRow, source_document_id=r.id)
    ]
    assert not exts, "no extraction may run after a failed upload"
    assert not fetch_rows(db_path, ExtractedFactRow), "no facts may persist after a failed upload"

    # The failure is surfaced to the caller: either it raised, or the returned
    # status reads as failed — never a silent success.
    if not raised and result is not None:
        status = opt_field(result, "status", default=None)
        if status is not None:
            assert "fail" in str(getattr(status, "value", status)).lower(), (
                f"the returned status must surface the failure (got {status!r})"
            )
