"""feat_api criterion 6 — JSON logging wired: dictConfig is active and handling
a request emits at least one log record that parses as JSON and carries the
request's ``correlation_id`` (the same id echoed on the X-Correlation-ID
response header). These structured records are what feeds the phi_check corpus.

FROZEN GOALS. Capture is at the file-descriptor level (capfd), so any
stream-handler destination (stdout/stderr) counts; one JSON object per line.
"""

from __future__ import annotations

import json

CLINICIAN_ID = 9001


def _json_records(text: str) -> list[dict]:
    records: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not (line.startswith("{") and line.endswith("}")):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            records.append(obj)
    return records


def test_api_06_json_logs_with_correlation_id(client, start_rounds, upload_document, capfd):
    start_rounds(client, [1001])
    chat = client.post(
        "/v1/chat",
        json={
            "clinician_id": CLINICIAN_ID,
            "patient_id": 1001,
            "message": "What is the latest troponin value?",
        },
    )
    upload = upload_document(client, 1001)

    request_ids = {
        resp.headers.get("x-correlation-id") for resp in (chat, upload)
    } - {None, ""}
    assert request_ids, (
        "responses must echo X-Correlation-ID (Week-1 middleware contract) so "
        "log records can be correlated"
    )

    captured = capfd.readouterr()
    records = _json_records(captured.out + "\n" + captured.err)
    with_cid = [rec for rec in records if rec.get("correlation_id")]
    assert with_cid, (
        "JSON logging must be WIRED (logging.config.dictConfig active): handling "
        "a request must emit at least one single-line JSON log record carrying a "
        f"'correlation_id' key; captured {len(records)} JSON record(s), none "
        "with correlation_id"
    )
    assert any(rec.get("correlation_id") in request_ids for rec in with_cid), (
        "at least one captured JSON log record must carry the correlation_id of "
        f"the request that produced it (expected one of {sorted(request_ids)}; "
        f"saw {sorted({rec['correlation_id'] for rec in with_cid})[:5]})"
    )
