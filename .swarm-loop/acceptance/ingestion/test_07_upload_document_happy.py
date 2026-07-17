"""feat_ingestion criterion 7 — upload_document happy path + content-hash dedupe.

`OpenEmrWriteClient.upload_document` multipart-POSTs to the Standard API
document route and returns a usable OpenEMR document id; through the pipeline,
re-submitting identical bytes dedupes on content hash — exactly one OpenEMR
upload, one stored openemr_document_id. FROZEN GOAL HARNESS.
"""

from __future__ import annotations

import pytest

from ._helpers import (
    build_fixture_pdf,
    far_future_token,
    fetch_rows,
    opt_field,
    resolve_attach,
    upload_flex,
)


async def test_07_upload_document_and_dedupe(db_path, tmp_path, settings, fake_openemr):
    from copilot.fhir.auth import StaticTokenProvider
    from copilot.fhir.write_client import OpenEmrWriteClient
    from copilot.memory.models import SourceDocumentRow

    # --- part A: direct client upload — multipart POST + usable id back ---
    provider = StaticTokenProvider(token=far_future_token())
    pdf = build_fixture_pdf(("Hemoglobin 13.5 g/dL",))
    async with OpenEmrWriteClient(fake_openemr.STANDARD_API_BASE_URL, provider) as client:
        result = await upload_flex(client, patient_pid=1001, content=pdf, filename="direct.pdf")

    calls = [c for c in fake_openemr.WRITE_CALLS if c["resource"] == "document"]
    assert len(calls) == 1, f"expected exactly one document POST (got {len(calls)})"
    call = calls[0]
    assert call["method"] == "POST" and call["patient_id"] == "1001" and call["has_body"]
    assert call["content_type"].lower().startswith("multipart/form-data"), (
        f"the Standard-API document upload is multipart (got {call['content_type']!r})"
    )

    if isinstance(result, (str, int)):
        doc_ref = str(result)
    else:
        doc_ref = str(
            opt_field(result, "openemr_document_id", "document_id", "new_id", "id", "uuid", default="")
        )
    assert doc_ref.strip(), "upload_document must return a usable OpenEMR document id"
    assert any(ch.isdigit() for ch in doc_ref), f"unexpected document id {doc_ref!r}"

    # --- part B: content-hash dedupe through the pipeline (idempotent retry) ---
    fake_openemr.WRITE_CALLS.clear()
    attach = resolve_attach(settings, tmp_path)
    pdf2 = build_fixture_pdf(("Sodium 140 mmol/L",))
    await attach(patient_pid=1003, content=pdf2, doc_type="lab_pdf", filename="dedupe.pdf")
    await attach(patient_pid=1003, content=pdf2, doc_type="lab_pdf", filename="dedupe.pdf")

    doc_calls = [
        c
        for c in fake_openemr.WRITE_CALLS
        if c["resource"] == "document" and c["patient_id"] == "1003"
    ]
    assert len(doc_calls) == 1, (
        "content-hash dedupe: identical bytes must be uploaded to OpenEMR exactly once "
        f"(got {len(doc_calls)} uploads)"
    )

    rows = fetch_rows(db_path, SourceDocumentRow, patient_id=1003)
    assert rows, "the ingested document must be recorded in the agent store"
    stored_ids = {r.openemr_document_id for r in rows if r.openemr_document_id}
    assert len(stored_ids) == 1, (
        f"exactly one openemr_document_id must be stored for the deduped bytes "
        f"(got {stored_ids or 'none'})"
    )
    if len(rows) > 1:
        hashes = {r.content_hash for r in rows}
        assert len(hashes) == 1, "identical bytes must share one content hash"
