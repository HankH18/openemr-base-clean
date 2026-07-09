"""feat_chat — grounded conversational drill-down (UC-2) + graceful uncertainty (UC-7).

FROZEN GOALS. Black-box over HTTP. Each test = one acceptance criterion; the
per-feature metric is the count that pass. They fail cleanly (wrong status / shape)
until the chat + rounds-session + serve-time-verification features are built.

Frozen HTTP contract exercised here:
- POST /v1/rounds/start {clinician_id, patient_ids:[int]} -> 200; establishes the
  clinician's authorized rounding session. (Chat requires an established session.)
- POST /v1/chat {clinician_id, patient_id, message, conversation_id?, correlation_id?}
  -> 200 {answer, claims:[{text, source_ref:{resource_type,resource_id,field,value}}],
          verification:{action in served|withheld|degraded, passed:bool},
          conversation_id:int, correlation_id:str}
- GET /v1/conversations/{id} -> 200 {messages:[{role, content}, ...]}
"""

from __future__ import annotations

from _fake_openemr import TROPONIN_VALUE

CLIN = 9001
SICK = 1001  # NSTEMI, critical troponin present in fixtures


def _start(client, patient_ids=(1001, 1002, 1004)):
    r = client.post("/v1/rounds/start", json={"clinician_id": CLIN, "patient_ids": list(patient_ids)})
    assert r.status_code == 200, f"POST /v1/rounds/start -> {r.status_code} (session/authz not built)"
    return r


def _chat(client, message, patient_id=SICK, conversation_id=None, correlation_id=None):
    body = {"clinician_id": CLIN, "patient_id": patient_id, "message": message}
    if conversation_id is not None:
        body["conversation_id"] = conversation_id
    if correlation_id is not None:
        body["correlation_id"] = correlation_id
    return client.post("/v1/chat", json=body)


def test_chat_endpoint_responds(client):
    _start(client)
    r = _chat(client, "What is the latest troponin?")
    assert r.status_code == 200, f"POST /v1/chat -> {r.status_code}"


def test_chat_claims_carry_source_ref(client):
    _start(client)
    r = _chat(client, "What is the latest troponin?")
    assert r.status_code == 200
    body = r.json()
    assert body.get("claims"), "answer must carry at least one grounded claim"
    for claim in body["claims"]:
        ref = claim["source_ref"]
        assert set(ref) >= {"resource_type", "resource_id", "field", "value"}


def test_chat_present_data_is_served_and_grounded(client):
    _start(client)
    r = _chat(client, "What is the latest troponin value?")
    assert r.status_code == 200
    body = r.json()
    assert body["verification"]["action"] == "served"
    # A claim must cite the real troponin value from source (grounding, not phrasing).
    values = {c["source_ref"]["value"] for c in body["claims"]}
    assert TROPONIN_VALUE in values, f"expected grounded troponin {TROPONIN_VALUE}, got {values}"


def test_chat_absent_data_withheld_gracefully(client):
    """UC-7: asked about data not in the record, the agent withholds rather than guessing."""
    _start(client)
    r = _chat(client, "What did the patient's MRI brain show?")
    assert r.status_code == 200
    body = r.json()
    assert body["verification"]["action"] == "withheld"
    assert body.get("answer"), "a withheld answer must still say something honest (surface uncertainty)"


def test_chat_multiturn_conversation_persisted(client):
    _start(client)
    first = _chat(client, "What is the latest troponin?")
    assert first.status_code == 200
    conv_id = first.json()["conversation_id"]
    second = _chat(client, "And is she on aspirin?", conversation_id=conv_id)
    assert second.status_code == 200
    assert second.json()["conversation_id"] == conv_id
    hist = client.get(f"/v1/conversations/{conv_id}")
    assert hist.status_code == 200
    messages = hist.json()["messages"]
    # at least the two user turns are persisted (assistant turns optional)
    assert sum(1 for m in messages if m["role"] == "user") >= 2


def test_chat_correlation_id_echoed(client):
    _start(client)
    cid = "acc-corr-12345678"
    r = _chat(client, "What is the latest troponin?", correlation_id=cid)
    assert r.status_code == 200
    assert r.json().get("correlation_id") == cid
