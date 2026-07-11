"""FHIR fixture bundles that mirror the seed data.

One helper per patient scenario the eval suite exercises.  Kept as Python
code (not JSON files) so the fixtures can share small helpers and stay
readable — these aren't recorded traces, they're intentional test cases.
"""


def observation(
    id: str,
    label: str,
    loinc: str,
    value: float,
    unit: str,
    abnormal: str = "",
    last_updated: str = "2026-07-08T03:00:00Z",
    patient: str = "1015",
    effective: str | None = None,
) -> dict:
    """Build a US-Core-shaped Observation.

    ``effective`` defaults to None so existing cases carry no ``effectiveDateTime``
    (``extract_temporal`` → None ⇒ the temporal gate is skipped); set it to
    exercise temporal grounding/drift.
    """
    obs = {
        "resourceType": "Observation",
        "id": id,
        "status": "final",
        "subject": {"reference": f"Patient/{patient}"},
        "code": {
            "text": label,
            "coding": [{"system": "http://loinc.org", "code": loinc, "display": label}],
        },
        "valueQuantity": {"value": value, "unit": unit},
        "interpretation": ([{"coding": [{"code": abnormal}]}] if abnormal else []),
        "meta": {"lastUpdated": last_updated},
    }
    if effective is not None:
        obs["effectiveDateTime"] = effective
    return obs


def medication_request(
    id: str,
    name: str,
    patient: str,
    active: bool = True,
    last_updated: str = "2026-07-08T00:00:00Z",
    authored_on: str | None = None,
) -> dict:
    med = {
        "resourceType": "MedicationRequest",
        "id": id,
        "status": "active" if active else "stopped",
        "intent": "order",
        "subject": {"reference": f"Patient/{patient}"},
        "medicationCodeableConcept": {"text": name, "coding": [{"display": name}]},
        "meta": {"lastUpdated": last_updated},
    }
    if authored_on is not None:
        med["authoredOn"] = authored_on
    return med


def allergy(
    id: str,
    name: str,
    patient: str,
    active: bool = True,
    last_updated: str = "2026-07-07T00:00:00Z",
) -> dict:
    return {
        "resourceType": "AllergyIntolerance",
        "id": id,
        "clinicalStatus": {"coding": [{"code": "active" if active else "inactive"}]},
        "code": {"text": name, "coding": [{"display": name}]},
        "patient": {"reference": f"Patient/{patient}"},
        "meta": {"lastUpdated": last_updated},
    }


# --- Patient 1015: r/o ACS with an overnight critical trop rise ------------


def pt1015_overnight_change_bundle() -> list[dict]:
    """The scripted overnight-deterioration patient."""
    return [
        observation(
            id="trop-baseline",
            label="Troponin I",
            loinc="6598-7",
            value=0.02,
            unit="ng/mL",
            abnormal="",
            last_updated="2026-07-07T05:00:00Z",
        ),
        observation(
            id="trop-overnight",
            label="Troponin I",
            loinc="6598-7",
            value=2.34,
            unit="ng/mL",
            abnormal="HH",  # US Core critical high
            last_updated="2026-07-08T03:00:00Z",
        ),
    ]


# --- Patient 1006: cellulitis, PCN allergy + amoxi-clav Rx (conflict) ------


def pt1006_drug_allergy_conflict_bundle() -> list[dict]:
    return [
        allergy(id="pcn-1006", name="Penicillin", patient="1006"),
        medication_request(id="amox-1006", name="Amoxicillin-clavulanate", patient="1006"),
        medication_request(id="cephx-1006", name="Cephalexin", patient="1006"),
    ]


# --- Patient 1004: sepsis with critical lactate ---------------------------


def pt1004_severe_sepsis_bundle() -> list[dict]:
    return [
        observation(
            id="lact-1004",
            label="Lactate",
            loinc="32693-4",
            value=4.2,
            unit="mmol/L",
            abnormal="HH",
            last_updated="2026-07-07T20:00:00Z",
            patient="1004",
        ),
        observation(
            id="wbc-1004",
            label="WBC",
            loinc="6690-2",
            value=18.6,
            unit="K/uL",
            abnormal="H",
            last_updated="2026-07-07T20:00:00Z",
            patient="1004",
        ),
    ]


# --- Patient 1003: DKA with critical K + glucose ---------------------------


def pt1003_dka_bundle() -> list[dict]:
    return [
        observation(
            id="k-1003",
            label="Potassium",
            loinc="2823-3",
            value=5.7,
            unit="mEq/L",
            abnormal="HH",
            patient="1003",
        ),
        observation(
            id="glucose-1003",
            label="Glucose",
            loinc="2345-7",
            value=386,
            unit="mg/dL",
            abnormal="HH",
            patient="1003",
        ),
    ]
