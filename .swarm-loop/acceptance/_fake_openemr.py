"""Deterministic fake OpenEMR for the frozen Week-2 acceptance suite.

FROZEN GOAL HARNESS — do not edit to make a test pass. Extends the Week-1 fake
(FHIR read + OAuth token) with the OpenEMR **Standard REST API** write routes the
Week-2 build targets:

- ``POST …/api/patient/{pid}/document``        — multipart source-document upload.
- ``POST …/api/patient/{pid}/medical_problem`` — physician-confirmed problem write.
- ``POST …/api/patient/{pid}/allergy``         — physician-confirmed allergy write.

Every write POST is recorded in ``WRITE_CALLS`` (path, patient id, content type,
headers) so tests can assert the agent hit the right route with a multipart body /
idempotency key. Create responses carry a rich id envelope (root ``id`` +
``data.id`` + ``uuid`` + a document-specific ``document_id``) so a client is not
brittle to which id key it reads. ``DOCUMENT_UPLOAD_MODE`` lets a test force the
document route to fail (500) for the fail-closed ingestion path.

The router is lenient (matches host ``openemr.test``; ``testserver`` TestClient
traffic passes through), exactly like the Week-1 fake, so implementations stay
free to choose base paths / query shapes.
"""

from __future__ import annotations

import copy
import re
from typing import Any

import httpx
import respx

FHIR_HOST = "http://openemr.test"
FHIR_BASE_URL = f"{FHIR_HOST}/fhir"
OAUTH_TOKEN_URL = f"{FHIR_HOST}/oauth2/default/token"
OAUTH_AUTHORIZE_URL = f"{FHIR_HOST}/oauth2/default/authorize"
# Standard REST API base (the write client derives this by swapping /fhir → /api).
STANDARD_API_BASE_URL = f"{FHIR_HOST}/api"

_KNOWN_TYPES = {
    "Patient", "Encounter", "Observation", "DiagnosticReport",
    "MedicationRequest", "MedicationStatement", "Condition",
    "AllergyIntolerance", "Practitioner",
}

# Fixed timestamps — no wall-clock (determinism).
_RECENT = "2026-07-09T06:30:00Z"
_OLDER = "2026-07-08T20:00:00Z"

_ABNORMAL_SYS = "http://terminology.hl7.org/CodeSystem/v3-ObservationInterpretation"

# --- mutable test-observable state ------------------------------------------
# Reset by the conftest ``fake_openemr`` fixture between tests.
WRITE_CALLS: list[dict[str, Any]] = []
DOCUMENT_UPLOAD_MODE = "ok"  # "ok" | "error"
_ID_COUNTER = {"n": 5000}


def reset_state() -> None:
    """Clear recorded write calls + restore default modes (per-test isolation)."""
    WRITE_CALLS.clear()
    global DOCUMENT_UPLOAD_MODE
    DOCUMENT_UPLOAD_MODE = "ok"
    _ID_COUNTER["n"] = 5000


def _next_id() -> int:
    _ID_COUNTER["n"] += 1
    return _ID_COUNTER["n"]


def _obs(rid: str, code_text: str, value: float, unit: str, low: float, high: float,
         interp: str | None, updated: str = _RECENT) -> dict[str, Any]:
    r: dict[str, Any] = {
        "resourceType": "Observation",
        "id": rid,
        "meta": {"lastUpdated": updated},
        "status": "final",
        "code": {"text": code_text},
        "valueQuantity": {"value": value, "unit": unit},
        "referenceRange": [{"low": {"value": low}, "high": {"value": high}}],
    }
    if interp is not None:
        r["interpretation"] = [{"coding": [{"system": _ABNORMAL_SYS, "code": interp}]}]
    return r


def _med(rid: str, name: str, status: str = "active", updated: str = _OLDER) -> dict[str, Any]:
    return {
        "resourceType": "MedicationRequest",
        "id": rid,
        "meta": {"lastUpdated": updated},
        "status": status,
        "medicationCodeableConcept": {"text": name},
    }


def _allergy(rid: str, substance: str, updated: str = _OLDER) -> dict[str, Any]:
    return {
        "resourceType": "AllergyIntolerance",
        "id": rid,
        "meta": {"lastUpdated": updated},
        "clinicalStatus": {"coding": [{"code": "active"}]},
        "code": {"text": substance},
    }


def _cond(rid: str, name: str, updated: str = _OLDER) -> dict[str, Any]:
    return {
        "resourceType": "Condition",
        "id": rid,
        "meta": {"lastUpdated": updated},
        "clinicalStatus": {"coding": [{"code": "active"}]},
        "code": {"text": name},
    }


def _patient(pid: str) -> dict[str, Any]:
    return {"resourceType": "Patient", "id": pid, "meta": {"lastUpdated": _OLDER}}


# --- Synthetic cohort (Week-1 parity) ---------------------------------------
PATIENTS: dict[str, dict[str, list[dict[str, Any]]]] = {
    "1001": {
        "Patient": [_patient("1001")],
        "Observation": [_obs("obs-1001-trop", "Troponin I", 0.9, "ng/mL", 0.0, 0.04, "HH")],
        "MedicationRequest": [_med("med-1001-asa", "aspirin")],
        "Condition": [_cond("cond-1001", "NSTEMI")],
    },
    "1002": {
        "Patient": [_patient("1002")],
        "Observation": [_obs("obs-1002-k", "Potassium", 5.6, "mmol/L", 3.5, 5.1, "H")],
        "MedicationRequest": [_med("med-1002-lisin", "lisinopril")],
    },
    "1003": {
        "Patient": [_patient("1003")],
        "Observation": [_obs("obs-1003-hgb", "Hemoglobin", 13.5, "g/dL", 12.0, 16.0, None)],
        "MedicationRequest": [_med("med-1003-amox", "amoxicillin")],
        "AllergyIntolerance": [_allergy("alg-1003-pcn", "penicillin")],
    },
    "1004": {
        "Patient": [_patient("1004")],
        "Observation": [_obs("obs-1004-na", "Sodium", 140.0, "mmol/L", 135.0, 145.0, None)],
        "MedicationRequest": [_med("med-1004-omep", "omeprazole")],
    },
    "1005": {
        "Patient": [_patient("1005")],
        "Observation": [_obs("obs-1005-lactate", "Lactate", 5.0, "mmol/L", 0.5, 2.0, "HH", _RECENT)],
        "MedicationRequest": [_med("med-1005-ivf", "sodium chloride 0.9% IV")],
        "Condition": [_cond("cond-1005", "Sepsis")],
    },
}

RESOURCES_BY_ID: dict[tuple[str, str], dict[str, Any]] = {}
for _pid, _bytype in PATIENTS.items():
    for _rtype, _rlist in _bytype.items():
        for _r in _rlist:
            RESOURCES_BY_ID[(_rtype, _r["id"])] = _r

CRITICAL_PATIENTS = {"1001", "1005"}
TROPONIN_VALUE = "0.9"
FABRICATED_ABSENT_TOPIC = "MRI"


def _last_updated(res: dict[str, Any]) -> str:
    return str(res.get("meta", {}).get("lastUpdated", _OLDER))


def _resources_for(rtype: str, pid: str | None, params: httpx.QueryParams) -> list[dict[str, Any]]:
    if pid is not None:
        pool = PATIENTS.get(pid, {}).get(rtype, [])
    else:
        pool = [r for byt in PATIENTS.values() for r in byt.get(rtype, [])]
    raw = params.get("_lastUpdated")
    if raw and raw.startswith("gt"):
        gt = raw[2:]
        pool = [r for r in pool if _last_updated(r) > gt]
    return pool


def _handle_get(request: httpx.Request) -> httpx.Response:
    parts = [p for p in request.url.path.split("/") if p]
    if parts and parts[-1] == "metadata":
        return httpx.Response(200, json={"resourceType": "CapabilityStatement", "fhirVersion": "4.0.1"})
    idx = next((i for i, s in enumerate(parts) if s in _KNOWN_TYPES), None)
    if idx is None:
        return httpx.Response(200, json={"resourceType": "Bundle", "type": "searchset", "total": 0, "entry": []})
    rtype = parts[idx]
    rid = parts[idx + 1] if len(parts) > idx + 1 else None
    if rid:
        res = RESOURCES_BY_ID.get((rtype, rid))
        if res is not None:
            return httpx.Response(200, json=res)
        return httpx.Response(404, json={"resourceType": "OperationOutcome"})
    params = request.url.params
    patient = params.get("patient") or params.get("subject")
    pid = patient.split("/")[-1] if patient else None
    resources = _resources_for(rtype, pid, params)
    if params.get("_summary") == "count":
        return httpx.Response(200, json={"resourceType": "Bundle", "type": "searchset", "total": len(resources)})
    entry = [{"resource": r, "fullUrl": f"{FHIR_BASE_URL}/{rtype}/{r['id']}"} for r in resources]
    return httpx.Response(200, json={"resourceType": "Bundle", "type": "searchset", "total": len(entry), "entry": entry})


def _handle_token(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"access_token": "fake-acceptance-token", "token_type": "Bearer", "expires_in": 3600})


def _id_envelope(kind: str, new_id: int) -> dict[str, Any]:
    """A forgiving id envelope covering the id-key conventions a client may read."""
    body: dict[str, Any] = {
        "id": new_id,
        "uuid": f"uuid-{new_id}",
        "data": {"id": new_id, "uuid": f"uuid-{new_id}"},
    }
    if kind == "document":
        body["document_id"] = new_id
        body["data"]["document_id"] = new_id
    if kind == "vital":
        body["vid"] = new_id
    return body


def _handle_post_api(request: httpx.Request) -> httpx.Response:
    """Dispatch a Standard-API create by its trailing resource segment."""
    parts = [p for p in request.url.path.split("/") if p]
    resource = parts[-1] if parts else ""
    pid = None
    if "patient" in parts:
        i = parts.index("patient")
        if i + 1 < len(parts):
            pid = parts[i + 1]
    content_type = request.headers.get("content-type", "")
    WRITE_CALLS.append({
        "method": "POST",
        "path": request.url.path,
        "resource": resource,
        "patient_id": pid,
        "content_type": content_type,
        "idempotency_key": request.headers.get("idempotency-key"),
        "has_body": bool(request.content),
    })
    if resource == "document" and DOCUMENT_UPLOAD_MODE == "error":
        return httpx.Response(500, json={"error": "simulated OpenEMR upload failure"})
    kind = "document" if resource == "document" else ("vital" if resource == "vital" else resource)
    return httpx.Response(201, json=_id_envelope(kind, _next_id()))


def _handle_put_api(request: httpx.Request) -> httpx.Response:
    parts = [p for p in request.url.path.split("/") if p]
    WRITE_CALLS.append({"method": "PUT", "path": request.url.path, "resource": parts[-1] if parts else ""})
    return httpx.Response(200, json=_id_envelope("update", _next_id()))


def build_router() -> respx.MockRouter:
    """A respx router intercepting only host openemr.test; testserver passes through."""
    router = respx.mock(assert_all_called=False, assert_all_mocked=False)
    router.route(method="POST", url__regex=re.escape(FHIR_HOST) + r".*(token|oauth).*").mock(side_effect=_handle_token)
    router.route(method="POST", url__regex=re.escape(FHIR_HOST) + r".*").mock(side_effect=_handle_post_api)
    router.route(method="PUT", url__regex=re.escape(FHIR_HOST) + r".*").mock(side_effect=_handle_put_api)
    router.route(method="GET", url__regex=re.escape(FHIR_HOST) + r".*").mock(side_effect=_handle_get)
    return router


# Keep an unused import-safe alias so ``copy`` is referenced (parity with the
# Week-1 eval fake, which deep-copies drift fixtures); harmless and explicit.
_ = copy
