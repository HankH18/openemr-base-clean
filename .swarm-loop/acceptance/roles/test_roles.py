"""feat_roles — role-based access control (the doc's physician/nurse/resident model).

FROZEN GOALS, black-box over HTTP. The demo model: a request carries the clinician's
role in an `X-Clinician-Role` header; **leading a round** (POST /v1/rounds/start) is
restricted to clinicians who round — `physician` or `resident`. A `nurse` (view/assist
role) is refused, and an unrecognized role is refused. Absent header defaults to
`physician` so the pre-existing (role-unaware) flows stay backward-compatible.

Baseline: no role model exists, so every role is treated the same and the restricted
cases return 200 — these fail until RBAC is enforced.
"""

from __future__ import annotations

CLIN = 8802


def _start(client, role):
    headers = {"X-Clinician-Role": role} if role is not None else {}
    return client.post(
        "/v1/rounds/start",
        json={"clinician_id": CLIN, "patient_ids": [1001]},
        headers=headers,
    )


def test_physician_role_may_lead_a_round(client):
    assert _start(client, "physician").status_code == 200


def test_nurse_role_may_not_lead_a_round(client):
    r = _start(client, "nurse")
    assert r.status_code == 403, (
        f"a nurse role must not be able to start/lead a round; got {r.status_code}"
    )


def test_unrecognized_role_is_refused(client):
    r = _start(client, "wizard")
    assert r.status_code == 403, (
        f"an unrecognized clinical role must be refused; got {r.status_code}"
    )
