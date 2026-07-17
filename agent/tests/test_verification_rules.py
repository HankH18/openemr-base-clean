"""Domain-rule tests: allergy-med conflict + critical/abnormal labs."""

from __future__ import annotations

from copilot.verification.core import build_context_from_resources
from copilot.verification.rules import allergy_medication_conflict, critical_lab


def _allergy(id: str, name: str, active: bool = True) -> dict:
    return {
        "resourceType": "AllergyIntolerance",
        "id": id,
        "clinicalStatus": {"coding": [{"code": "active" if active else "inactive"}]},
        "code": {"text": name, "coding": [{"display": name}]},
    }


def _med(id: str, name: str, status: str = "active") -> dict:
    return {
        "resourceType": "MedicationRequest",
        "id": id,
        "status": status,
        "medicationCodeableConcept": {"text": name, "coding": [{"display": name}]},
    }


def _obs(id: str, label: str, value: float, unit: str, code: str) -> dict:
    return {
        "resourceType": "Observation",
        "id": id,
        "status": "final",
        "code": {"text": label},
        "valueQuantity": {"value": value, "unit": unit},
        "interpretation": [{"coding": [{"code": code}]}],
    }


class TestAllergyMedicationConflict:
    def test_pcn_allergy_conflicts_with_amoxicillin_clavulanate(self) -> None:
        """Reproduces Pt 1006 in the seed: PCN allergy + amox-clav Rx."""
        ctx = build_context_from_resources(
            [
                _allergy("a-1", "Penicillin"),
                _med("m-1", "Amoxicillin-clavulanate"),
                _med("m-2", "Cephalexin"),  # different class — no conflict
            ]
        )
        flags = allergy_medication_conflict(ctx)
        assert len(flags) == 1
        assert flags[0].rule == "allergy_medication_conflict"
        assert flags[0].severity == "critical"
        assert flags[0].must_surface is True
        assert "Amoxicillin" in flags[0].message
        assert "Penicillin" in flags[0].message

    def test_sulfa_allergy_flags_bactrim(self) -> None:
        ctx = build_context_from_resources(
            [
                _allergy("a-1", "Sulfa drugs"),
                _med("m-1", "Bactrim"),
            ]
        )
        flags = allergy_medication_conflict(ctx)
        assert len(flags) == 1

    def test_inactive_allergy_ignored(self) -> None:
        ctx = build_context_from_resources(
            [
                _allergy("a-1", "Penicillin", active=False),
                _med("m-1", "Amoxicillin"),
            ]
        )
        assert allergy_medication_conflict(ctx) == []

    def test_inactive_med_ignored(self) -> None:
        ctx = build_context_from_resources(
            [
                _allergy("a-1", "Penicillin"),
                _med("m-1", "Amoxicillin", status="stopped"),
            ]
        )
        assert allergy_medication_conflict(ctx) == []

    def test_nkda_line_does_not_produce_flags(self) -> None:
        ctx = build_context_from_resources(
            [
                _allergy("a-1", "No known drug allergies"),
                _med("m-1", "Amoxicillin"),
            ]
        )
        assert allergy_medication_conflict(ctx) == []


class TestCriticalLab:
    def test_critical_high_from_us_core_hh(self) -> None:
        ctx = build_context_from_resources([_obs("obs-1", "Troponin I", 2.34, "ng/mL", "HH")])
        flags = critical_lab(ctx)
        assert len(flags) == 1
        assert flags[0].rule == "critical_lab"
        assert flags[0].severity == "critical"
        assert flags[0].must_surface is True
        assert "Troponin" in flags[0].message
        assert "critically high" in flags[0].message

    def test_critical_low_from_ll(self) -> None:
        ctx = build_context_from_resources([_obs("obs-1", "Sodium", 118, "mEq/L", "LL")])
        flags = critical_lab(ctx)
        assert flags[0].severity == "critical"
        assert "critically low" in flags[0].message

    def test_openemr_critical_high_string_recognized(self) -> None:
        """Seed uses `abnormal='critical_high'` — verify direct-path fallback."""
        raw = {
            "resourceType": "Observation",
            "id": "obs-1",
            "code": {"text": "Troponin I"},
            "valueQuantity": {"value": 2.34, "unit": "ng/mL"},
            "abnormal": "critical_high",
        }
        ctx = build_context_from_resources([raw])
        flags = critical_lab(ctx)
        assert len(flags) == 1
        assert flags[0].severity == "critical"

    def test_non_critical_abnormal_flagged_as_warning_but_not_must_surface(self) -> None:
        ctx = build_context_from_resources([_obs("obs-1", "Creatinine", 1.4, "mg/dL", "H")])
        flags = critical_lab(ctx)
        assert flags[0].rule == "abnormal_lab"
        assert flags[0].severity == "warning"
        assert flags[0].must_surface is False

    def test_normal_lab_produces_no_flag(self) -> None:
        # Empty interpretation, no abnormal key
        raw = {
            "resourceType": "Observation",
            "id": "obs-1",
            "code": {"text": "WBC"},
            "valueQuantity": {"value": 8.4, "unit": "K/uL"},
        }
        ctx = build_context_from_resources([raw])
        assert critical_lab(ctx) == []
