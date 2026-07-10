"""feat_audit — every PHI read writes an append-only audit row (HIPAA §164.312(b)).

FROZEN GOALS, black-box: drive the HTTP surface, then read the `audit_log` table
back with a plain sync SQLite connection (avoids cross-event-loop async issues).
Baseline: record_audit exists but is never called, so the table stays empty and
these fail — that IS the target.
"""

from __future__ import annotations

import os
import sqlite3

CLIN = 8801


def _audit_rows() -> list[dict[str, object]]:
    url = os.environ["COPILOT_DATABASE_URL"]  # sqlite+aiosqlite:///<path>
    path = url.split(":///", 1)[-1]
    con = sqlite3.connect(path)
    try:
        cur = con.execute(
            "SELECT action, patient_id, clinician_id, correlation_id FROM audit_log"
        )
        cols = ("action", "patient_id", "clinician_id", "correlation_id")
        return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]
    finally:
        con.close()


def test_chat_read_writes_an_audit_row(client):
    assert (
        client.post(
            "/v1/rounds/start", json={"clinician_id": CLIN, "patient_ids": [1001]}
        ).status_code
        == 200
    )
    r = client.post(
        "/v1/chat",
        json={"clinician_id": CLIN, "patient_id": 1001, "message": "latest troponin?"},
    )
    assert r.status_code == 200
    rows = _audit_rows()
    assert any(
        row["patient_id"] == 1001 and row["clinician_id"] == CLIN for row in rows
    ), "a chat PHI read must write an audit row naming the patient + clinician"


def test_rounds_start_audits_each_patient_read(client):
    assert (
        client.post(
            "/v1/rounds/start", json={"clinician_id": CLIN, "patient_ids": [1001, 1002]}
        ).status_code
        == 200
    )
    patients = {row["patient_id"] for row in _audit_rows()}
    assert {1001, 1002} <= patients, "rounds/start must audit every patient chart it reads"


def test_audit_rows_carry_a_correlation_id(client):
    client.post("/v1/rounds/start", json={"clinician_id": CLIN, "patient_ids": [1001]})
    client.post(
        "/v1/chat",
        json={"clinician_id": CLIN, "patient_id": 1001, "message": "x"},
        headers={"X-Correlation-ID": "audit-corr-12345678"},
    )
    rows = _audit_rows()
    assert rows, "expected at least one audit row"
    assert all(
        isinstance(row["correlation_id"], str) and row["correlation_id"] for row in rows
    ), "every audit row must record the request correlation id for traceability"
