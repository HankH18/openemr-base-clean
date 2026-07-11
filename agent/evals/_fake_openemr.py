"""Deterministic in-process fake OpenEMR FHIR server for the eval runner.

Self-contained copy of the acceptance-suite fake-OpenEMR pattern (see
``.swarm-loop/acceptance/_fake_openemr.py``) — deliberately duplicated here so
``run_evals.py`` never imports from the frozen ``.swarm-loop`` tree. It defines a
synthetic clinical ground truth and a lenient ``respx`` router that intercepts
the agent's outbound HTTP to OpenEMR while letting the in-process ``TestClient``
traffic (host ``testserver``) pass through.

Extension over the acceptance fake: patient ``1007`` is a *record-drift* case —
its Troponin reads back **by ID** with a value that differs from what the search
Bundle reported, so a chat answer grounded on the search value fails the
serve-time re-fetch value-match and is correctly ``withheld``. This is the
deterministic analogue of "the live record changed since the agent read it".

@package   OpenEMR
@link      https://www.open-emr.org
@license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
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

_KNOWN_TYPES = {
    "Patient", "Encounter", "Observation", "DiagnosticReport",
    "MedicationRequest", "MedicationStatement", "Condition",
    "AllergyIntolerance", "Practitioner",
}

# Fixed timestamps — no wall-clock (determinism).
_RECENT = "2026-07-09T06:30:00Z"
_OLDER = "2026-07-08T20:00:00Z"

_ABNORMAL_SYS = "http://terminology.hl7.org/CodeSystem/v3-ObservationInterpretation"


def _obs(rid: str, code_text: str, value: float, unit: str, low: float, high: float,
         interp: str | None, updated: str = _RECENT,
         effective: str | None = None) -> dict[str, Any]:
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
    # Optional clinical time — default None keeps existing cases temporal-field-free
    # so `extract_temporal` returns None and the temporal gate is skipped.
    if effective is not None:
        r["effectiveDateTime"] = effective
    return r


def _med(rid: str, name: str, status: str = "active", updated: str = _OLDER,
         authored_on: str | None = None) -> dict[str, Any]:
    r: dict[str, Any] = {
        "resourceType": "MedicationRequest",
        "id": rid,
        "meta": {"lastUpdated": updated},
        "status": status,
        "medicationCodeableConcept": {"text": name},
    }
    if authored_on is not None:
        r["authoredOn"] = authored_on
    return r


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


# --- Synthetic cohort -------------------------------------------------------
# 1001: NSTEMI, CRITICAL troponin (HH)             -> highest acuity
# 1002: hyperkalemia, WARNING potassium (H)        -> mid acuity
# 1003: penicillin allergy + active amoxicillin    -> allergy-med conflict, normal labs
# 1004: stable, normal                             -> low acuity
# 1005: sepsis, CRITICAL lactate (HH), recent      -> deterioration/alert patient
# 1007: record-drift case (search value != read-by-id value) -> chat WITHHELD
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
    "1007": {
        "Patient": [_patient("1007")],
        # Search Bundle reports 0.9; the by-ID re-fetch below reports 2.5.
        "Observation": [_obs("obs-1007-trop", "Troponin I", 0.9, "ng/mL", 0.0, 0.04, None)],
    },
}

# Flat index for read-by-id (serve-time re-fetch).
RESOURCES_BY_ID: dict[tuple[str, str], dict[str, Any]] = {}
for _pid, _bytype in PATIENTS.items():
    for _rtype, _rlist in _bytype.items():
        for _r in _rlist:
            RESOURCES_BY_ID[(_rtype, _r["id"])] = _r

# --- Record-drift override --------------------------------------------------
# Patient 1007's Troponin reads back by ID with a *different* value than the
# search Bundle advertised. A chat claim grounded on the search value ("0.9")
# therefore fails the serve-time value-match against the live re-fetch ("2.5"),
# so the fail-closed gate withholds — the deterministic "record drifted" path.
_drifted_trop = copy.deepcopy(RESOURCES_BY_ID[("Observation", "obs-1007-trop")])
_drifted_trop["valueQuantity"]["value"] = 2.5
RESOURCES_BY_ID[("Observation", "obs-1007-trop")] = _drifted_trop

# Convenient assertions the dataset/runner rely on.
CRITICAL_PATIENTS = {"1001", "1005"}   # interpretation HH
TROPONIN_VALUE = "0.9"                  # present, groundable for 1001
DRIFT_PATIENT = "1007"                  # search value != read-by-id value
FABRICATED_ABSENT_TOPIC = "MRI"         # no imaging in any fixture -> ungroundable


def _last_updated(res: dict[str, Any]) -> str:
    updated = res.get("meta", {}).get("lastUpdated", _OLDER)
    return str(updated)


def _resources_for(rtype: str, pid: str | None, params: httpx.QueryParams) -> list[dict[str, Any]]:
    if pid is not None:
        pool = PATIENTS.get(pid, {}).get(rtype, [])
    else:
        pool = [r for byt in PATIENTS.values() for r in byt.get(rtype, [])]
    gt = None
    raw = params.get("_lastUpdated")
    if raw and raw.startswith("gt"):
        gt = raw[2:]
    if gt:
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
    return httpx.Response(200, json={"access_token": "fake-eval-token", "token_type": "Bearer", "expires_in": 3600})


def build_router() -> respx.MockRouter:
    """A respx router intercepting only host openemr.test; testserver passes through."""
    router = respx.mock(assert_all_called=False, assert_all_mocked=False)
    router.route(method="POST", url__regex=re.escape(FHIR_HOST) + r".*(token|oauth).*").mock(side_effect=_handle_token)
    router.route(method="GET", url__regex=re.escape(FHIR_HOST) + r".*").mock(side_effect=_handle_get)
    return router
