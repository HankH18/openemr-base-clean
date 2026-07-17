"""feat_api criterion 2 — status + page endpoints: `GET /v1/documents/{id}`
returns status/facts/citations; `GET .../pages/{n}` serves the page image;
correct 404s for an unknown document and an unknown page.

FROZEN GOALS, black-box over HTTP. The stubbed in-process pipeline (stub OCR,
stub vision, respx-faked OpenEMR) must reach status="extracted" for the fixture
PDF; the poll below is bounded (~20s worst case), normally immediate.
"""

from __future__ import annotations

import time

DOC_STATUSES = {"uploaded", "extracting", "extracted", "failed"}
TERMINAL = {"extracted", "failed"}


def test_api_02_status_facts_citations_page_image_404s(client, start_rounds, upload_document):
    start_rounds(client, [1001])
    up = upload_document(client, 1001)
    assert up.status_code == 202, (
        f"precondition: upload must be accepted (202); got {up.status_code}: {up.text[:300]}"
    )
    doc_id = up.json()["document_id"]

    body: dict = {}
    status: str | None = None
    for _ in range(80):  # bounded poll for the async stub pipeline
        g = client.get(f"/v1/documents/{doc_id}")
        assert g.status_code == 200, (
            f"GET /v1/documents/{{id}} -> {g.status_code}: {g.text[:300]}"
        )
        body = g.json()
        status = body.get("status")
        assert status in DOC_STATUSES, (
            f"status must be one of {sorted(DOC_STATUSES)}; got {status!r}"
        )
        if status in TERMINAL:
            break
        time.sleep(0.25)
    assert status == "extracted", (
        f"the stubbed ingestion of the fixture PDF must reach status='extracted' "
        f"(deterministic happy path); ended at {status!r}"
    )

    for key in ("status", "doc_type", "page_count", "extraction", "citations"):
        assert key in body, f"status payload must carry {key!r}; got keys {sorted(body)}"
    assert body["doc_type"] == "lab_pdf", f"doc_type must echo the upload; got {body['doc_type']!r}"
    assert isinstance(body["page_count"], int) and body["page_count"] >= 1, (
        f"page_count must be a positive int; got {body['page_count']!r}"
    )
    extraction = body["extraction"]
    assert isinstance(extraction, dict) and isinstance(extraction.get("facts"), list), (
        f"extraction must be an object carrying a 'facts' list; got {extraction!r}"
    )
    assert isinstance(body["citations"], list), "citations must be a list"

    # Page image (the bbox-overlay backdrop).
    page = client.get(f"/v1/documents/{doc_id}/pages/1")
    assert page.status_code == 200, f"GET .../pages/1 -> {page.status_code}"
    ctype = page.headers.get("content-type", "")
    assert ctype.startswith("image/"), (
        f"the page endpoint must serve the rendered page image; got content-type {ctype!r}"
    )
    assert page.content, "page image body must be non-empty"

    # Correct 404s.
    missing_doc = client.get("/v1/documents/99999999")
    assert missing_doc.status_code == 404, (
        f"unknown document id must 404; got {missing_doc.status_code}"
    )
    missing_page = client.get(f"/v1/documents/{doc_id}/pages/9999")
    assert missing_page.status_code == 404, (
        f"unknown page number must 404; got {missing_page.status_code}"
    )
