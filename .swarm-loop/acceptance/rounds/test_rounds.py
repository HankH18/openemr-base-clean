"""feat_rounds — start-of-day top patient (UC-1), guided hand-off (UC-3),
interrogable ranking (UC-4). FROZEN GOALS, black-box over HTTP.

Contract:
- POST /v1/rounds/start {clinician_id, patient_ids:[int]} -> 200; body carries the
  current PatientCard (top-level or under "current").
- GET  /v1/rounds/current?clinician_id=int -> 200 current PatientCard.
- POST /v1/rounds/advance {clinician_id, completed_patient_id} -> 200 next card
  (or {"done": true} when the list is exhausted).
PatientCard fields (see copilot.domain.contracts.PatientCard): patient_id,
summary_claims[], acuity_score, rank_reason, freshness{as_of, age_seconds, stale}.
"""

from __future__ import annotations

CLIN = 9001


def _pid(value):
    """PatientId may serialise as int or {"value": int} — accept either."""
    return value["value"] if isinstance(value, dict) else int(value)


def _card(body: dict) -> dict:
    return body["current"] if isinstance(body, dict) and "current" in body else body


def _start(client, patient_ids):
    return client.post("/v1/rounds/start", json={"clinician_id": CLIN, "patient_ids": list(patient_ids)})


def test_rounds_start_returns_patient_card(client):
    r = _start(client, [1004, 1002, 1001])
    assert r.status_code == 200, f"POST /v1/rounds/start -> {r.status_code}"
    card = _card(r.json())
    assert "patient_id" in card
    assert isinstance(card.get("summary_claims"), list)
    assert "acuity_score" in card and "rank_reason" in card
    fresh = card["freshness"]
    assert {"as_of", "age_seconds", "stale"} <= set(fresh)


def test_rounds_start_ranks_sickest_first(client):
    # Unsorted list; 1001 is the only critical (HH troponin) -> must be presented first.
    r = _start(client, [1004, 1002, 1001])
    assert r.status_code == 200
    assert _pid(_card(r.json())["patient_id"]) == 1001


def test_rounds_current_matches_start(client):
    s = _start(client, [1004, 1002, 1001])
    assert s.status_code == 200
    top = _pid(_card(s.json())["patient_id"])
    r = client.get("/v1/rounds/current", params={"clinician_id": CLIN})
    assert r.status_code == 200
    assert _pid(_card(r.json())["patient_id"]) == top


def test_rounds_advance_returns_next_by_acuity(client):
    _start(client, [1004, 1002, 1001])
    r = client.post("/v1/rounds/advance", json={"clinician_id": CLIN, "completed_patient_id": 1001})
    assert r.status_code == 200, f"POST /v1/rounds/advance -> {r.status_code}"
    card = _card(r.json())
    # next-sickest after 1001 (critical) is 1002 (warning) ahead of 1004 (normal)
    assert _pid(card["patient_id"]) == 1002


def test_rounds_cursor_survives_reload(client, make_client):
    _start(client, [1004, 1002, 1001])
    client.post("/v1/rounds/advance", json={"clinician_id": CLIN, "completed_patient_id": 1001})
    # a brand-new app instance on the SAME database must resume at the advanced cursor
    fresh_client = make_client()
    r = fresh_client.get("/v1/rounds/current", params={"clinician_id": CLIN})
    assert r.status_code == 200
    assert _pid(_card(r.json())["patient_id"]) == 1002


def test_rounds_rank_reason_is_grounded(client):
    """UC-4: the ranking must be interrogable — a non-empty reason backed by evidence."""
    r = _start(client, [1004, 1002, 1001])
    assert r.status_code == 200
    card = _card(r.json())
    assert card["rank_reason"].strip(), "rank_reason must explain why this patient is first"
    assert card["summary_claims"], "top card must carry grounded evidence claims"
    for claim in card["summary_claims"]:
        assert {"resource_type", "resource_id", "field", "value"} <= set(claim["source_ref"])
