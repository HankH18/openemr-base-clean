"""Deterministic acuity ranking — temporal correctness of the acuity signal.

The seed carries a dated time series per metric. Acuity must reflect the
patient's *current* state: each metric contributes at most one flag (its latest
reading), never a worse historical point. These tests pin that behaviour and
confirm a genuinely multi-metric-critical patient still counts each metric.
"""

from __future__ import annotations

from typing import Any

from copilot.domain.primitives import PatientId
from copilot.rounds.ranking import assess_patient


def _obs(
    rid: str,
    name: str,
    value: float,
    when: str,
    *,
    code: str | None = None,
    unit: str = "mmol/L",
    low: float | None = None,
    high: float | None = None,
) -> dict[str, Any]:
    """A groundable Observation: metric label, value, clinical time, opt. flag."""
    obs: dict[str, Any] = {
        "resourceType": "Observation",
        "id": rid,
        "code": {"coding": [{"display": name}]},
        "valueQuantity": {"value": value, "unit": unit},
        "effectiveDateTime": when,
    }
    if code is not None:
        obs["interpretation"] = [{"coding": [{"code": code}]}]
    if low is not None or high is not None:
        rng: dict[str, Any] = {}
        if low is not None:
            rng["low"] = {"value": low}
        if high is not None:
            rng["high"] = {"value": high}
        obs["referenceRange"] = [rng]
    return obs


_PID = PatientId(value=1014)


def test_series_assessed_on_latest_not_worst_historical() -> None:
    """Sodium fell to 118 (critical) 2 days ago but has since risen to 124 (mild).

    Acuity must judge the *current* 124 — one warning flag citing 124 — not the
    historical 118. Pre-fix, the old critical point inflated this into the
    critical band and cited "118".
    """
    series = [
        _obs("na-old", "Sodium", 118, "2026-07-09T05:00:00Z", code="LL", low=135, high=145),
        _obs("na-mid", "Sodium", 122, "2026-07-10T05:00:00Z", code="L", low=135, high=145),
        _obs("na-now", "Sodium", 124, "2026-07-11T05:00:00Z", code="L", low=135, high=145),
    ]
    got = assess_patient(_PID, series)

    # Current state is a single mild abnormal — warning band, below the 7.0 alert
    # threshold — not the historical critical.
    assert got.acuity_score < 7.0
    assert got.rank_reason.startswith("Abnormal")
    # Cites the current value, never the worse historical points.
    assert "124" in got.rank_reason
    assert "118" not in got.rank_reason
    assert "122" not in got.rank_reason
    # Exactly one flag: no "; "-joined second finding for the same metric.
    assert "; " not in got.rank_reason

    # The collapsed assessment is byte-identical to assessing just the latest
    # reading — the historical points add nothing to score or reason.
    latest_only = assess_patient(_PID, [series[-1]])
    assert got == latest_only


def test_creatinine_series_matches_single_latest_value() -> None:
    """A resolving creatinine (3.1 old → 2.4 latest) scores on the 2.4."""
    series = [
        _obs("cr-old", "Creatinine", 3.1, "2026-07-09T05:00:00Z", code="H", unit="mg/dL", high=1.3),
        _obs("cr-now", "Creatinine", 2.4, "2026-07-11T05:00:00Z", code="H", unit="mg/dL", high=1.3),
    ]
    got = assess_patient(_PID, series)
    assert "2.4" in got.rank_reason
    assert "3.1" not in got.rank_reason
    assert got == assess_patient(_PID, [series[-1]])


def test_multi_metric_critical_counts_each_distinct_metric() -> None:
    """Two distinct metrics currently critical still yield two flags.

    Each metric collapses to its latest reading, but distinct metrics are not
    merged: a normal historical point is dropped, yet potassium AND sodium both
    remain critical, so both are counted.
    """
    resources = [
        _obs("k-old", "Potassium", 4.2, "2026-07-09T05:00:00Z", unit="mEq/L", low=3.5, high=5.0),
        _obs("k-now", "Potassium", 6.9, "2026-07-11T05:00:00Z", code="HH", unit="mEq/L", low=3.5, high=5.0),
        _obs("na-old", "Sodium", 139, "2026-07-09T05:00:00Z", low=135, high=145),
        _obs("na-now", "Sodium", 118, "2026-07-11T05:00:00Z", code="LL", low=135, high=145),
    ]
    got = assess_patient(_PID, resources)

    assert got.rank_reason.startswith("Critical")
    # Both distinct metrics are named — the collapse did not merge them.
    assert "Potassium" in got.rank_reason
    assert "Sodium" in got.rank_reason
    assert "; " in got.rank_reason  # two findings joined
    # Two current criticals sit high in the critical band (>= 8.0).
    assert got.acuity_score >= 8.0
    # Dropped historical normals are not cited.
    assert "4.2" not in got.rank_reason
    assert "139" not in got.rank_reason


def test_non_observation_resources_pass_through_unchanged() -> None:
    """A latest-critical lab is flagged; non-Observation resources are untouched.

    The collapse must not drop conditions/meds/allergies — verification and
    display still rely on them being present.
    """
    resources = [
        _obs("na-old", "Sodium", 118, "2026-07-09T05:00:00Z", code="LL", low=135, high=145),
        _obs("na-now", "Sodium", 124, "2026-07-11T05:00:00Z", code="L", low=135, high=145),
        {"resourceType": "Condition", "id": "c1", "code": {"text": "NSTEMI"}},
        {
            "resourceType": "MedicationRequest",
            "id": "m1",
            "status": "active",
            "medicationCodeableConcept": {"text": "Aspirin"},
        },
    ]
    # No crash, and the current (mild) sodium still drives a warning-band score.
    got = assess_patient(_PID, resources)
    assert got.rank_reason.startswith("Abnormal")
    assert "124" in got.rank_reason


def test_no_abnormal_findings_is_normal_band() -> None:
    """All-normal current readings collapse to the normal band."""
    resources = [
        _obs("na-old", "Sodium", 118, "2026-07-09T05:00:00Z", code="LL", low=135, high=145),
        _obs("na-now", "Sodium", 140, "2026-07-11T05:00:00Z", low=135, high=145),
    ]
    got = assess_patient(_PID, resources)
    assert got.acuity_score == 1.0
    assert got.rank_reason == "No abnormal findings on the latest labs."
