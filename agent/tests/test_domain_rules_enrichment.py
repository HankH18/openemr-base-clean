"""Tests for the additive domain rules: reference-range + med reconciliation.

Both rules are appended to ``default_rules()`` and must be strictly additive:
they surface findings the existing rules miss without altering existing rule
output, the verification action, or the acuity ranking.  The seeded acceptance
cohort deliberately keeps them silent (every out-of-range lab carries an
interpretation code, and no patient has a ``MedicationStatement``) — the last
two tests here reproduce that invariant directly.
"""

from __future__ import annotations

from typing import Any

from copilot.verification.core import build_context_from_resources
from copilot.verification.rules import (
    allergy_medication_conflict,
    critical_lab,
    default_rules,
    medication_reconciliation,
    reference_range,
)


def _obs(
    id: str,
    label: str,
    value: float,
    low: float | None = None,
    high: float | None = None,
    unit: str = "mg/dL",
    interp: str | None = None,
) -> dict[str, Any]:
    r: dict[str, Any] = {
        "resourceType": "Observation",
        "id": id,
        "status": "final",
        "code": {"text": label},
        "valueQuantity": {"value": value, "unit": unit},
    }
    if low is not None or high is not None:
        rng: dict[str, Any] = {}
        if low is not None:
            rng["low"] = {"value": low}
        if high is not None:
            rng["high"] = {"value": high}
        r["referenceRange"] = [rng]
    if interp is not None:
        r["interpretation"] = [{"coding": [{"code": interp}]}]
    return r


def _med_request(id: str, name: str, status: str = "active") -> dict[str, Any]:
    return {
        "resourceType": "MedicationRequest",
        "id": id,
        "status": status,
        "medicationCodeableConcept": {"text": name},
    }


def _med_statement(id: str, name: str, status: str = "active") -> dict[str, Any]:
    return {
        "resourceType": "MedicationStatement",
        "id": id,
        "status": status,
        "medicationCodeableConcept": {"text": name},
    }


class TestReferenceRange:
    def test_above_high_with_no_interpretation_is_flagged(self) -> None:
        ctx = build_context_from_resources([_obs("o-1", "Creatinine", 3.2, low=0.6, high=1.3)])
        flags = reference_range(ctx)
        assert len(flags) == 1
        assert flags[0].rule == "reference_range"
        assert flags[0].severity == "warning"
        assert flags[0].must_surface is False
        assert "Creatinine" in flags[0].message
        assert "above the reference range" in flags[0].message
        assert "0.6-1.3" in flags[0].message
        assert flags[0].evidence[0].field == "valueQuantity.value"
        assert flags[0].evidence[0].value == "3.2"

    def test_below_low_with_no_interpretation_is_flagged(self) -> None:
        ctx = build_context_from_resources(
            [_obs("o-1", "Potassium", 2.1, low=3.5, high=5.1, unit="mmol/L")]
        )
        flags = reference_range(ctx)
        assert len(flags) == 1
        assert "below the reference range" in flags[0].message

    def test_in_range_value_produces_no_flag(self) -> None:
        ctx = build_context_from_resources(
            [_obs("o-1", "Sodium", 140.0, low=135.0, high=145.0, unit="mmol/L")]
        )
        assert reference_range(ctx) == []

    def test_value_at_boundary_is_in_range(self) -> None:
        # Exactly on low/high is not out of range (strict comparison).
        ctx = build_context_from_resources(
            [
                _obs("o-low", "A", 3.5, low=3.5, high=5.1),
                _obs("o-high", "B", 5.1, low=3.5, high=5.1),
            ]
        )
        assert reference_range(ctx) == []

    def test_out_of_range_but_already_interpreted_is_not_duplicated(self) -> None:
        # Mirrors seed pt 1002: out of range AND carries an 'H' code, so the
        # existing abnormal_lab rule owns it — reference_range must stay silent.
        obs = _obs("o-1", "Potassium", 5.6, low=3.5, high=5.1, unit="mmol/L", interp="H")
        ctx = build_context_from_resources([obs])
        assert reference_range(ctx) == []
        # ...but the existing rule still flags it, unchanged.
        existing = critical_lab(ctx)
        assert len(existing) == 1
        assert existing[0].rule == "abnormal_lab"

    def test_critical_interpretation_is_not_duplicated(self) -> None:
        obs = _obs("o-1", "Troponin I", 0.9, low=0.0, high=0.04, unit="ng/mL", interp="HH")
        ctx = build_context_from_resources([obs])
        assert reference_range(ctx) == []
        assert critical_lab(ctx)[0].rule == "critical_lab"

    def test_missing_reference_range_produces_no_flag(self) -> None:
        ctx = build_context_from_resources([_obs("o-1", "WBC", 8.4)])
        assert reference_range(ctx) == []

    def test_only_high_bound_below_it_is_in_range(self) -> None:
        ctx = build_context_from_resources([_obs("o-1", "TSH", 2.0, high=4.0)])
        assert reference_range(ctx) == []

    def test_only_high_bound_above_it_is_flagged(self) -> None:
        ctx = build_context_from_resources([_obs("o-1", "TSH", 9.0, high=4.0)])
        flags = reference_range(ctx)
        assert len(flags) == 1
        assert "above the reference range" in flags[0].message
        assert "<= 4" in flags[0].message

    def test_only_low_bound_below_it_is_flagged(self) -> None:
        ctx = build_context_from_resources([_obs("o-1", "Glucose", 40.0, low=70.0)])
        flags = reference_range(ctx)
        assert len(flags) == 1
        assert "below the reference range" in flags[0].message
        assert ">= 70" in flags[0].message

    def test_non_numeric_value_is_ignored(self) -> None:
        obs: dict[str, Any] = {
            "resourceType": "Observation",
            "id": "o-1",
            "code": {"text": "Blood group"},
            "valueQuantity": {"value": "A+", "unit": ""},
            "referenceRange": [{"low": {"value": 1}, "high": {"value": 2}}],
        }
        ctx = build_context_from_resources([obs])
        assert reference_range(ctx) == []

    def test_unrecognized_interpretation_code_does_not_suppress_check(self) -> None:
        # 'N' (normal) is not a code the existing rule handles, so a genuinely
        # out-of-range value still gets surfaced.
        obs = _obs("o-1", "Calcium", 12.5, low=8.5, high=10.5, interp="N")
        ctx = build_context_from_resources([obs])
        flags = reference_range(ctx)
        assert len(flags) == 1
        assert flags[0].rule == "reference_range"


class TestMedicationReconciliation:
    def test_order_absent_from_reported_list_is_flagged(self) -> None:
        ctx = build_context_from_resources(
            [
                _med_request("mr-1", "aspirin"),
                _med_request("mr-2", "lisinopril"),
                _med_statement("ms-1", "aspirin"),  # keeps both stores populated
            ]
        )
        flags = medication_reconciliation(ctx)
        assert len(flags) == 1
        assert flags[0].rule == "medication_reconciliation"
        assert flags[0].severity == "warning"
        assert flags[0].must_surface is True
        assert "lisinopril" in flags[0].message
        assert "absent from the reported medication list" in flags[0].message

    def test_reported_med_without_order_is_flagged(self) -> None:
        ctx = build_context_from_resources(
            [
                _med_request("mr-1", "aspirin"),
                _med_statement("ms-1", "aspirin"),
                _med_statement("ms-2", "metformin"),
            ]
        )
        flags = medication_reconciliation(ctx)
        assert len(flags) == 1
        assert "metformin" in flags[0].message
        assert "no matching order" in flags[0].message

    def test_active_inactive_mismatch_is_flagged(self) -> None:
        ctx = build_context_from_resources(
            [
                _med_request("mr-1", "warfarin", status="active"),
                _med_statement("ms-1", "warfarin", status="completed"),
            ]
        )
        flags = medication_reconciliation(ctx)
        assert len(flags) == 1
        assert "status disagrees" in flags[0].message
        assert "active order" in flags[0].message
        assert "inactive" in flags[0].message
        # Both stores are cited as evidence.
        kinds = {e.resource_type.value for e in flags[0].evidence}
        assert kinds == {"MedicationRequest", "MedicationStatement"}

    def test_agreeing_lists_produce_no_flags(self) -> None:
        ctx = build_context_from_resources(
            [
                _med_request("mr-1", "aspirin"),
                _med_request("mr-2", "lisinopril"),
                _med_statement("ms-1", "aspirin"),
                _med_statement("ms-2", "lisinopril"),
            ]
        )
        assert medication_reconciliation(ctx) == []

    def test_name_matching_is_case_and_whitespace_insensitive(self) -> None:
        ctx = build_context_from_resources(
            [
                _med_request("mr-1", "Aspirin"),
                _med_statement("ms-1", "  aspirin "),
            ]
        )
        assert medication_reconciliation(ctx) == []

    def test_no_statement_store_means_nothing_to_reconcile(self) -> None:
        # The seeded acceptance cohort shape: orders but no reported list.
        ctx = build_context_from_resources(
            [_med_request("mr-1", "aspirin"), _med_request("mr-2", "lisinopril")]
        )
        assert medication_reconciliation(ctx) == []

    def test_no_order_store_means_nothing_to_reconcile(self) -> None:
        ctx = build_context_from_resources(
            [_med_statement("ms-1", "aspirin"), _med_statement("ms-2", "metformin")]
        )
        assert medication_reconciliation(ctx) == []

    def test_multiple_divergences_each_flagged(self) -> None:
        ctx = build_context_from_resources(
            [
                _med_request("mr-1", "aspirin"),  # order-only
                _med_request("mr-2", "warfarin", status="active"),  # mismatch
                _med_statement("ms-1", "metformin"),  # statement-only
                _med_statement("ms-2", "warfarin", status="stopped"),  # mismatch
            ]
        )
        flags = medication_reconciliation(ctx)
        assert len(flags) == 3
        assert all(f.rule == "medication_reconciliation" for f in flags)


class TestDefaultRulesWiring:
    def test_default_rules_appends_new_rules_after_existing(self) -> None:
        rules = default_rules()
        assert len(rules) == 4
        # Existing rules kept, in their original order and position.
        assert rules[0] is allergy_medication_conflict
        assert rules[1] is critical_lab
        # New rules appended.
        assert rules[2] is reference_range
        assert rules[3] is medication_reconciliation

    def test_seed_style_cohort_stays_silent_for_new_rules(self) -> None:
        # Reproduces the acceptance invariant: every out-of-range seed lab has
        # an interpretation code, and no patient carries a MedicationStatement,
        # so neither new rule fires — the frozen suite is unaffected.
        bundle = [
            _obs("obs-1002-k", "Potassium", 5.6, low=3.5, high=5.1, unit="mmol/L", interp="H"),
            _obs("obs-1001-trop", "Troponin I", 0.9, low=0.0, high=0.04, unit="ng/mL", interp="HH"),
            _obs("obs-1004-na", "Sodium", 140.0, low=135.0, high=145.0, unit="mmol/L"),
            _med_request("med-1002-lisin", "lisinopril"),
        ]
        ctx = build_context_from_resources(bundle)
        assert reference_range(ctx) == []
        assert medication_reconciliation(ctx) == []
