"""feat_background — the poll/synthesis/persistence loop and proactive deterioration
alerts (UC-5), plus verification-at-synthesis. FROZEN GOALS, black-box over HTTP.

Contract:
- POST /v1/rounds/refresh {clinician_id} -> 200 {"results":[{"patient_id", "outcome", ...}]}
  runs one poll+verify+persist tick over the clinician's active list.
- GET  /v1/rounds/alerts?clinician_id=int -> 200 {"alerts":[{"patient_id", "reason"}]}.
"""

from __future__ import annotations

CLIN = 9001


def _pid(value):
    return value["value"] if isinstance(value, dict) else int(value)


def _card(body: dict) -> dict:
    return body["current"] if isinstance(body, dict) and "current" in body else body


def _start(client, patient_ids):
    return client.post("/v1/rounds/start", json={"clinician_id": CLIN, "patient_ids": list(patient_ids)})


def _refresh(client):
    return client.post("/v1/rounds/refresh", json={"clinician_id": CLIN})


def test_refresh_runs_and_reports_per_patient(client):
    assert _start(client, [1001]).status_code == 200
    r = _refresh(client)
    assert r.status_code == 200, f"POST /v1/rounds/refresh -> {r.status_code}"
    results = r.json()["results"]
    assert any(_pid(x["patient_id"]) == 1001 and "outcome" in x for x in results)


def test_refresh_reflects_freshness_on_card(client):
    _start(client, [1001])
    assert _refresh(client).status_code == 200
    r = client.get("/v1/rounds/current", params={"clinician_id": CLIN})
    assert r.status_code == 200
    fresh = _card(r.json())["freshness"]
    assert isinstance(fresh["stale"], bool)
    assert isinstance(fresh["age_seconds"], int) and fresh["age_seconds"] >= 0


def test_alert_on_not_yet_seen_deterioration(client):
    # 1005 carries a CRITICAL lactate (HH) and is never advanced-to -> should preempt.
    assert _start(client, [1004, 1005]).status_code == 200
    assert _refresh(client).status_code == 200
    r = client.get("/v1/rounds/alerts", params={"clinician_id": CLIN})
    assert r.status_code == 200, f"GET /v1/rounds/alerts -> {r.status_code}"
    alerted = {_pid(a["patient_id"]) for a in r.json()["alerts"]}
    assert 1005 in alerted, f"expected a deterioration alert for 1005, got {alerted}"


def test_refresh_is_change_gated_idempotent(client):
    _start(client, [1001])
    assert _refresh(client).status_code == 200
    second = _refresh(client)
    assert second.status_code == 200
    for row in second.json()["results"]:
        assert "outcome" in row and not row.get("error")


def test_persisted_claims_are_grounded(client):
    # verification-at-synthesis: only grounded claims survive into the persisted card.
    _start(client, [1001])
    assert _refresh(client).status_code == 200
    r = client.get("/v1/rounds/current", params={"clinician_id": CLIN})
    assert r.status_code == 200
    claims = _card(r.json())["summary_claims"]
    assert claims, "a synthesized memory file must carry at least one grounded claim"
    for claim in claims:
        assert {"resource_type", "resource_id", "field", "value"} <= set(claim["source_ref"])
