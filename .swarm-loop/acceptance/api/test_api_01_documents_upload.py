"""feat_api criterion 1 — `POST /v1/documents` multipart -> 202 {document_id,
status, correlation_id}; auth required; RBAC rounding-list gate -> 403 off-list.

FROZEN GOALS, black-box over HTTP (W2_ARCHITECTURE.md "Interfaces & contracts").
"""

from __future__ import annotations


def test_api_01_post_documents_multipart_auth_rbac(client, start_rounds, upload_document):
    start_rounds(client, [1001, 1002])

    # On-list upload: 202 Accepted with the async-ingestion envelope.
    r = upload_document(client, 1001)
    assert r.status_code == 202, (
        f"POST /v1/documents (multipart, authorized, on-list) must return "
        f"202 Accepted; got {r.status_code}: {r.text[:300]}"
    )
    body = r.json()
    for key in ("document_id", "status", "correlation_id"):
        assert key in body, f"202 body must carry {key!r}; got keys {sorted(body)}"
    assert body["document_id"] not in (None, ""), "document_id must be non-empty"
    assert isinstance(body["status"], str) and body["status"], "status must be a non-empty string"
    assert isinstance(body["correlation_id"], str) and body["correlation_id"], (
        "correlation_id must be a non-empty string"
    )

    # RBAC rounding-list gate: patient 1003 is NOT on this clinician's list.
    off_list = upload_document(client, 1003)
    assert off_list.status_code == 403, (
        f"an upload for a patient off the clinician's rounding list must be "
        f"refused with 403; got {off_list.status_code}"
    )

    # Auth required: a clinician with no established rounding session is refused.
    no_session = upload_document(client, 1001, clinician_id=9999)
    assert no_session.status_code in (401, 403), (
        f"an upload without an established session/authorization must be "
        f"refused (401/403), got {no_session.status_code}"
    )
