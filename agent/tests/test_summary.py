"""Chart-summary builder: one row per metric, latest value + trend."""

from __future__ import annotations

from typing import Any

from copilot.rounds.summary import build_change_claims, build_summary_claims


def _obs(rid: str, name: str, value: float, when: str, unit: str = "/min") -> dict[str, Any]:
    return {
        "resourceType": "Observation",
        "id": rid,
        "code": {"coding": [{"display": name}]},
        "valueQuantity": {"value": value, "unit": unit},
        "effectiveDateTime": when,
    }


def test_collapses_to_one_per_metric_with_trend() -> None:
    resources = [
        _obs("hr-3", "Heart rate", 118, "2026-07-08T05:00:00Z"),
        _obs("hr-2", "Heart rate", 104, "2026-07-09T05:00:00Z"),
        _obs("hr-1", "Heart rate", 92, "2026-07-10T05:00:00Z"),  # latest
    ]
    claims = build_summary_claims(resources)
    assert len(claims) == 1
    text = claims[0].text
    assert text.startswith("Heart Rate: 92")  # humanized from raw "Heart rate"
    assert "↓12" in text  # 92 minus 104
    assert "24h since prior" in text
    assert claims[0].source_ref.resource_id == "hr-1"  # cites the latest reading


def test_rising_value_shows_up_arrow() -> None:
    resources = [
        _obs("g1", "Glucose", 42, "2026-07-09T05:00:00Z", unit="mg/dL"),
        _obs("g2", "Glucose", 60, "2026-07-10T05:00:00Z", unit="mg/dL"),
    ]
    text = build_summary_claims(resources)[0].text
    assert text.startswith("Glucose: 60 mg/dL")
    assert "↑18" in text
    assert "24h since prior" in text


def test_single_reading_has_no_trend() -> None:
    claims = build_summary_claims(
        [_obs("k1", "Potassium", 5.7, "2026-07-10T05:00:00Z", unit="mEq/L")]
    )
    assert len(claims) == 1
    assert claims[0].text == "Potassium: 5.7 mEq/L"


def test_no_change_reading() -> None:
    resources = [
        _obs("h2", "Body height", 71, "2026-07-09T05:00:00Z", unit="in"),
        _obs("h1", "Body height", 71, "2026-07-10T05:00:00Z", unit="in"),
    ]
    assert "no change" in build_summary_claims(resources)[0].text


def test_non_observation_passes_through_once() -> None:
    cond = {"resourceType": "Condition", "id": "c1", "code": {"text": "NSTEMI"}}
    claims = build_summary_claims([cond])
    assert len(claims) == 1
    assert "NSTEMI" in claims[0].text


def test_valueless_observation_is_skipped() -> None:
    panel = {"resourceType": "Observation", "id": "p1", "code": {"text": "Vitals panel"}}
    assert build_summary_claims([panel]) == []


# --- build_change_claims ("Since you last saw …", ~12h window) --------------


def _obs_abn(rid: str, name: str, value: float, when: str, code: str) -> dict[str, Any]:
    obs = _obs(rid, name, value, when)
    obs["interpretation"] = [{"coding": [{"code": code}]}]
    return obs


def test_change_includes_recent_abnormal_single_reading() -> None:
    obs = _obs_abn("k1", "Potassium", 5.7, "2026-07-10T05:00:00Z", "HH")
    claims = build_change_claims([obs])
    assert len(claims) == 1
    assert claims[0].text.startswith("Potassium: 5.7")


def test_change_includes_recent_changed_metric() -> None:
    resources = [
        _obs("hr-2", "Heart rate", 104, "2026-07-10T00:00:00Z"),
        _obs("hr-1", "Heart rate", 92, "2026-07-10T05:00:00Z"),  # 5h later, moved
    ]
    claims = build_change_claims(resources)
    assert len(claims) == 1
    assert "↓12" in claims[0].text


def test_change_excludes_stale_and_unchanged_normal() -> None:
    resources = [
        _obs_abn("glu", "Glucose", 386, "2026-07-10T05:00:00Z", "HH"),  # recent + abnormal
        _obs("ht2", "Body height", 71, "2026-07-10T00:00:00Z"),  # recent but unchanged/normal
        _obs("ht1", "Body height", 71, "2026-07-10T05:00:00Z"),
        _obs("wt", "Body weight", 180, "2026-07-08T05:00:00Z"),  # >12h before reference: stale
    ]
    labels = [c.text.split(":")[0] for c in build_change_claims(resources)]
    assert labels == ["Glucose"]


def test_change_empty_without_timestamps() -> None:
    obs = {
        "resourceType": "Observation",
        "id": "x",
        "code": {"coding": [{"display": "WBC"}]},
        "valueQuantity": {"value": 15.2, "unit": "K/uL"},
        "interpretation": [{"coding": [{"code": "H"}]}],
    }
    assert build_change_claims([obs]) == []


# --- record-grounded severity + trend classification -----------------------


def _obs_rng(
    rid: str,
    name: str,
    value: float,
    when: str,
    *,
    unit: str = "mmol/L",
    low: float | None = None,
    high: float | None = None,
    interp: str | None = None,
) -> dict[str, Any]:
    obs = _obs(rid, name, value, when, unit=unit)
    if low is not None or high is not None:
        band: dict[str, Any] = {}
        if low is not None:
            band["low"] = {"value": low}
        if high is not None:
            band["high"] = {"value": high}
        obs["referenceRange"] = [band]
    if interp is not None:
        obs["interpretation"] = [{"coding": [{"code": interp}]}]
    return obs


def _only(resources: list[dict[str, Any]]) -> Any:
    claims = build_summary_claims(resources)
    assert len(claims) == 1
    return claims[0]


def test_severity_normal_when_no_flag() -> None:
    claim = _only([_obs_rng("k1", "Potassium", 4.0, "2026-07-10T05:00:00Z", low=3.5, high=5.1)])
    assert claim.severity is not None and claim.severity.value == "normal"


def test_severity_warning_from_high_flag() -> None:
    claim = _only(
        [_obs_rng("k1", "Potassium", 5.6, "2026-07-10T05:00:00Z", low=3.5, high=5.1, interp="H")]
    )
    assert claim.severity is not None and claim.severity.value == "warning"


def test_severity_critical_from_double_letter_flag() -> None:
    claim = _only(
        [_obs_rng("l1", "Lactate", 5.0, "2026-07-10T05:00:00Z", low=0.5, high=2.0, interp="HH")]
    )
    assert claim.severity is not None and claim.severity.value == "critical"


def test_severity_critical_from_openemr_vhigh() -> None:
    obs = _obs_rng("t1", "Troponin I", 0.9, "2026-07-10T05:00:00Z", high=0.04)
    obs["abnormal"] = "vhigh"  # OpenEMR seed convention
    claim = _only([obs])
    assert claim.severity is not None and claim.severity.value == "critical"


def test_non_observation_claim_has_no_classification() -> None:
    cond = {"resourceType": "Condition", "id": "c1", "code": {"text": "NSTEMI"}}
    claim = _only([cond])
    assert claim.severity is None
    assert claim.trend_direction is None


def test_trend_worsening_moves_out_of_range() -> None:
    # One-sided lab band (troponin <0.04): prior in range, latest well above.
    resources = [
        _obs_rng("t-prior", "Troponin I", 0.02, "2026-07-09T05:00:00Z", unit="ng/mL", high=0.04),
        _obs_rng("t-now", "Troponin I", 0.9, "2026-07-10T05:00:00Z", unit="ng/mL", high=0.04),
    ]
    claim = _only(resources)
    assert claim.trend_direction is not None and claim.trend_direction.value == "worsening"


def test_trend_improving_shrinks_distance_to_range() -> None:
    resources = [
        _obs_rng("k-prior", "Potassium", 6.0, "2026-07-09T05:00:00Z", low=3.5, high=5.1),
        _obs_rng("k-now", "Potassium", 5.5, "2026-07-10T05:00:00Z", low=3.5, high=5.1),
    ]
    claim = _only(resources)
    assert claim.trend_direction is not None and claim.trend_direction.value == "improving"


def test_trend_steady_when_both_in_range() -> None:
    resources = [
        _obs_rng("k-prior", "Potassium", 4.5, "2026-07-09T05:00:00Z", low=3.5, high=5.1),
        _obs_rng("k-now", "Potassium", 4.0, "2026-07-10T05:00:00Z", low=3.5, high=5.1),
    ]
    claim = _only(resources)
    assert claim.trend_direction is not None and claim.trend_direction.value == "steady"


def test_trend_uses_vitals_table_when_record_has_no_range() -> None:
    # Heart rate carries no referenceRange -> standard adult band (60-100) applies.
    resources = [
        _obs("hr-prior", "Heart rate", 130, "2026-07-09T05:00:00Z"),
        _obs("hr-now", "Heart rate", 110, "2026-07-10T05:00:00Z"),
    ]
    claim = _only(resources)
    assert claim.trend_direction is not None and claim.trend_direction.value == "improving"


def test_trend_none_without_any_range() -> None:
    # A non-vital metric with no recorded range → neutral (no fabricated band).
    resources = [
        _obs("t-prior", "Troponin I", 0.02, "2026-07-09T05:00:00Z", unit="ng/mL"),
        _obs("t-now", "Troponin I", 0.9, "2026-07-10T05:00:00Z", unit="ng/mL"),
    ]
    claim = _only(resources)
    assert claim.trend_direction is None


def test_trend_none_for_single_reading() -> None:
    claim = _only([_obs_rng("k1", "Potassium", 5.7, "2026-07-10T05:00:00Z", low=3.5, high=5.1)])
    assert claim.trend_direction is None


# --- value_direction (raw up/down/none motion, independent of the range) -----


def test_value_direction_up_when_latest_rose() -> None:
    resources = [
        _obs("g1", "Glucose", 42, "2026-07-09T05:00:00Z", unit="mg/dL"),
        _obs("g2", "Glucose", 60, "2026-07-10T05:00:00Z", unit="mg/dL"),  # latest, higher
    ]
    claim = _only(resources)
    assert claim.value_direction is not None and claim.value_direction.value == "up"


def test_value_direction_down_when_latest_fell() -> None:
    resources = [
        _obs("hr-2", "Heart rate", 104, "2026-07-09T05:00:00Z"),
        _obs("hr-1", "Heart rate", 92, "2026-07-10T05:00:00Z"),  # latest, lower
    ]
    claim = _only(resources)
    assert claim.value_direction is not None and claim.value_direction.value == "down"


def test_value_direction_none_without_prior() -> None:
    claim = _only([_obs("k1", "Potassium", 5.7, "2026-07-10T05:00:00Z", unit="mEq/L")])
    assert claim.value_direction is not None and claim.value_direction.value == "none"


def test_value_direction_none_when_unchanged() -> None:
    resources = [
        _obs("h2", "Body height", 71, "2026-07-09T05:00:00Z", unit="in"),
        _obs("h1", "Body height", 71, "2026-07-10T05:00:00Z", unit="in"),  # unchanged
    ]
    claim = _only(resources)
    assert claim.value_direction is not None and claim.value_direction.value == "none"


def test_value_direction_independent_of_range() -> None:
    # Latest ROSE (42 -> 60) even though it moved back toward the low band: the
    # movement marker reports raw motion; the trend colour reports range-relative.
    resources = [
        _obs_rng("g1", "Glucose", 42, "2026-07-09T05:00:00Z", unit="mg/dL", low=70, high=99),
        _obs_rng("g2", "Glucose", 60, "2026-07-10T05:00:00Z", unit="mg/dL", low=70, high=99),
    ]
    claim = _only(resources)
    assert claim.value_direction is not None and claim.value_direction.value == "up"
    assert claim.trend_direction is not None and claim.trend_direction.value == "improving"


def test_non_observation_claim_has_no_value_direction() -> None:
    cond = {"resourceType": "Condition", "id": "c1", "code": {"text": "NSTEMI"}}
    assert _only([cond]).value_direction is None


def test_classification_round_trips_through_repository() -> None:
    from copilot.memory.repository import _claim_from_json, _claim_to_json

    resources = [
        _obs_rng("k-prior", "Potassium", 6.0, "2026-07-09T05:00:00Z", low=3.5, high=5.1, interp="H"),
        _obs_rng("k-now", "Potassium", 5.5, "2026-07-10T05:00:00Z", low=3.5, high=5.1, interp="H"),
    ]
    claim = _only(resources)
    back = _claim_from_json(_claim_to_json(claim))
    assert back.severity == claim.severity
    assert back.trend_direction == claim.trend_direction
    assert back.value_direction == claim.value_direction
    assert back.source_ref.value == claim.source_ref.value  # value stays verbatim


def test_memory_file_round_trips_changes() -> None:
    from datetime import datetime

    from copilot.domain.contracts import Claim, MemoryFileSummary
    from copilot.domain.primitives import FhirReference, PatientId, ResourceType
    from copilot.memory.models import MemoryFileRow
    from copilot.memory.repository import _row_to_summary, _summary_to_json

    claim = Claim(
        text="Potassium: 5.7 mEq/L",
        source_ref=FhirReference(
            resource_type=ResourceType.Observation,
            resource_id="k1",
            field="valueQuantity.value",
            value="5.7",
        ),
    )
    when = datetime(2026, 7, 10, 5, 0, 0)
    summary = MemoryFileSummary(
        patient_id=PatientId(value=1003),
        claims=[claim],
        changes=[claim],
        acuity_score=9.0,
        rank_reason="Critical: Potassium is critically high",
        synthesized_at=when,
        source_watermark=when,
        content_hash="a" * 64,
    )
    row = MemoryFileRow(
        patient_id=1003,
        summary=_summary_to_json(summary),
        acuity_score=summary.acuity_score,
        rank_reason=summary.rank_reason,
        synthesized_at=summary.synthesized_at,
        source_watermark=summary.source_watermark,
        content_hash=summary.content_hash,
    )
    back = _row_to_summary(row)
    assert len(back.changes) == 1
    assert back.changes[0].text == "Potassium: 5.7 mEq/L"
    assert back.changes[0].source_ref.resource_id == "k1"
