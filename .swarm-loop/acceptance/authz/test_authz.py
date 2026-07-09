"""feat_authz — authorization boundary (UC-6): the agent refuses patients outside
the clinician's authorized rounding list, and never leaks one patient into another's
conversation. FROZEN GOALS, black-box over HTTP.

Authorization model (demo): a clinician's authorized set = the patient_ids they
established via POST /v1/rounds/start. Serve-time re-check gates chat + rounds.
"""

from __future__ import annotations

from _fake_openemr import PATIENTS

CLIN = 9001


def _ids_for(pid: str) -> set[str]:
    return {r["id"] for lst in PATIENTS[pid].values() for r in lst}


def _start(client, patient_ids):
    return client.post("/v1/rounds/start", json={"clinician_id": CLIN, "patient_ids": list(patient_ids)})


def test_chat_about_unauthorized_patient_is_refused(client):
    # Session covers 1001 & 1002 only; 1003 is NOT authorized for this clinician.
    assert _start(client, [1001, 1002]).status_code == 200
    r = client.post("/v1/chat", json={"clinician_id": CLIN, "patient_id": 1003, "message": "summarize"})
    assert r.status_code == 403, f"expected 403 refusal for unauthorized patient, got {r.status_code}"


def test_current_requires_established_session(client):
    # Positive+negative so this can only pass once the endpoint EXISTS and enforces:
    # the clinician who started a session gets 200; a clinician who never did is refused.
    assert _start(client, [1001]).status_code == 200
    ok = client.get("/v1/rounds/current", params={"clinician_id": CLIN})
    assert ok.status_code == 200, f"authorized clinician should get a card, got {ok.status_code}"
    denied = client.get("/v1/rounds/current", params={"clinician_id": 9999})
    assert denied.status_code != 200, "a clinician with no rounding session must not receive a card"


def test_no_cross_patient_leakage(client):
    # Authorized for both, but a chat scoped to 1001 must not surface 1003's records.
    assert _start(client, [1001, 1003]).status_code == 200
    r = client.post(
        "/v1/chat",
        json={"clinician_id": CLIN, "patient_id": 1001, "message": "what medication is patient 1003 on?"},
    )
    assert r.status_code == 200, f"POST /v1/chat -> {r.status_code}"
    foreign = _ids_for("1003")
    cited = {c["source_ref"]["resource_id"] for c in r.json().get("claims", [])}
    assert not (cited & foreign), f"chat scoped to 1001 leaked 1003 resources: {cited & foreign}"
