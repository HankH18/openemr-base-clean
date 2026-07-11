"""Tests for the deterministic write-verification gate.

Two layers of defence, mirrored here:

- The **parse gate** — constructing a ``VitalWrite`` with an unknown metric or a
  non-numeric value fails at the type boundary (Pydantic), which is the first
  hard block (parse, don't validate).
- The **verify gate** — ``verify_write`` hard-blocks a wrong unit, soft-warns an
  out-of-physiologic-range human-direct value (overridable), and hard-blocks
  that same out-of-range value under the reserved strict mode.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from copilot.domain.primitives import ClinicianId, PatientId
from copilot.domain.writes import (
    MedicationWrite,
    VitalWrite,
    WritableMetric,
    WriteCandidate,
    WriteEntryMode,
    WriteKind,
)
from copilot.verification.writes import _spec_for, verify_write


def _vital_candidate(
    metric: WritableMetric,
    value: float,
    unit: str,
    *,
    entry_mode: WriteEntryMode = WriteEntryMode.human_direct,
) -> WriteCandidate:
    return WriteCandidate(
        kind=WriteKind.vital,
        patient_id=PatientId(value=1015),
        clinician_id=ClinicianId(value=7),
        idempotency_key="idem-1",
        entry_mode=entry_mode,
        vital=VitalWrite(metric=metric, value=value, unit=unit),
    )


def _med_candidate(title: str, begdate: str, enddate: str | None = None) -> WriteCandidate:
    return WriteCandidate(
        kind=WriteKind.medication,
        patient_id=PatientId(value=1015),
        clinician_id=ClinicianId(value=7),
        idempotency_key="idem-med",
        medication=MedicationWrite(title=title, begdate=begdate, enddate=enddate),
    )


# --- parse gate (type boundary) --------------------------------------------


class TestParseGate:
    def test_unknown_metric_is_rejected_at_construction(self) -> None:
        with pytest.raises(ValidationError):
            VitalWrite(metric="blood_glucose", value=90, unit="mg/dL")  # type: ignore[arg-type]

    def test_unparseable_value_is_rejected_at_construction(self) -> None:
        with pytest.raises(ValidationError):
            VitalWrite(metric=WritableMetric.heart_rate, value="not-a-number", unit="bpm")  # type: ignore[arg-type]

    def test_empty_unit_is_rejected_at_construction(self) -> None:
        with pytest.raises(ValidationError):
            VitalWrite(metric=WritableMetric.heart_rate, value=72, unit="")

    def test_candidate_requires_exactly_one_payload(self) -> None:
        with pytest.raises(ValidationError):
            WriteCandidate(
                kind=WriteKind.vital,
                patient_id=PatientId(value=1),
                clinician_id=ClinicianId(value=1),
                idempotency_key="k",
            )

    def test_candidate_payload_must_match_kind(self) -> None:
        with pytest.raises(ValidationError):
            WriteCandidate(
                kind=WriteKind.vital,
                patient_id=PatientId(value=1),
                clinician_id=ClinicianId(value=1),
                idempotency_key="k",
                medication=MedicationWrite(title="Aspirin", begdate="2026-07-11"),
            )


# --- unit sanity ------------------------------------------------------------


class TestUnitSanity:
    def test_wrong_unit_hard_blocks(self) -> None:
        verdict = verify_write(_vital_candidate(WritableMetric.heart_rate, 72, "mmHg"))
        assert verdict.blocked is True
        assert verdict.ok is False
        assert any("unit" in e for e in verdict.errors)
        assert verdict.warnings == []

    def test_matching_unit_passes_clean(self) -> None:
        verdict = verify_write(_vital_candidate(WritableMetric.heart_rate, 72, "bpm"))
        assert verdict.blocked is False
        assert verdict.ok is True
        assert verdict.warnings == []
        assert verdict.errors == []
        assert verdict.metric is WritableMetric.heart_rate

    def test_unit_normalization_is_alias_aware(self) -> None:
        # "°F", "F", "degF" all fold to the temperature canonical unit.
        for unit in ("°F", "F", "degF", "fahrenheit"):
            verdict = verify_write(_vital_candidate(WritableMetric.temperature, 98.6, unit))
            assert verdict.blocked is False, unit

    def test_percent_unit_for_spo2(self) -> None:
        verdict = verify_write(_vital_candidate(WritableMetric.spo2, 97, "%"))
        assert verdict.blocked is False


# --- physiologic plausibility ----------------------------------------------


class TestPlausibility:
    def test_out_of_range_is_soft_warning_for_human_direct(self) -> None:
        # 500 bpm is implausible, but a human typed it — recordable with a warning.
        verdict = verify_write(_vital_candidate(WritableMetric.heart_rate, 500, "bpm"))
        assert verdict.blocked is False
        assert verdict.ok is True
        assert len(verdict.warnings) == 1
        assert "physiologic range" in verdict.warnings[0]
        assert verdict.errors == []

    def test_out_of_range_hard_blocks_in_strict_mode(self) -> None:
        # The reserved Phase-2 mode hard-blocks the same out-of-range value.
        verdict = verify_write(
            _vital_candidate(WritableMetric.heart_rate, 500, "bpm"),
            mode=WriteEntryMode.agent_proposed_physician_confirmed,
        )
        assert verdict.blocked is True
        assert verdict.warnings == []
        assert any("physiologic range" in e for e in verdict.errors)

    def test_below_minimum_also_warns(self) -> None:
        verdict = verify_write(_vital_candidate(WritableMetric.heart_rate, 2, "bpm"))
        assert verdict.blocked is False
        assert len(verdict.warnings) == 1

    def test_boundary_values_are_in_range(self) -> None:
        spec = _spec_for(WritableMetric.spo2)
        for value in (spec.min_value, spec.max_value):
            verdict = verify_write(_vital_candidate(WritableMetric.spo2, value, "%"))
            assert verdict.blocked is False
            assert verdict.warnings == []

    def test_wrong_unit_and_out_of_range_still_blocks(self) -> None:
        # A hard unit error dominates; the verdict is blocked regardless of range.
        verdict = verify_write(_vital_candidate(WritableMetric.heart_rate, 500, "mmHg"))
        assert verdict.blocked is True


# --- every metric has a coherent spec --------------------------------------


class TestSpecTable:
    def test_every_metric_has_a_spec_with_valid_bounds(self) -> None:
        for metric in WritableMetric:
            spec = _spec_for(metric)
            assert spec.min_value < spec.max_value
            assert spec.canonical_unit
            # A mid-range value in the canonical unit is always clean.
            midpoint = (spec.min_value + spec.max_value) / 2
            verdict = verify_write(_vital_candidate(metric, midpoint, spec.canonical_unit))
            assert verdict.blocked is False, metric
            assert verdict.warnings == [], metric


# --- medication path --------------------------------------------------------


class TestMedication:
    def test_valid_medication_passes(self) -> None:
        verdict = verify_write(_med_candidate("Aspirin 81 mg", "2026-07-11"))
        assert verdict.blocked is False
        assert verdict.kind is WriteKind.medication

    def test_malformed_begdate_hard_blocks(self) -> None:
        verdict = verify_write(_med_candidate("Aspirin 81 mg", "July 11 2026"))
        assert verdict.blocked is True
        assert any("begdate" in e for e in verdict.errors)

    def test_malformed_enddate_hard_blocks(self) -> None:
        verdict = verify_write(_med_candidate("Aspirin", "2026-07-11", enddate="2026/07/12"))
        assert verdict.blocked is True
        assert any("enddate" in e for e in verdict.errors)
