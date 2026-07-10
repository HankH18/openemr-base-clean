"""Chart-summary builder: one row per metric, latest value + trend."""

from __future__ import annotations

from typing import Any

from copilot.rounds.summary import build_summary_claims


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
    assert text.startswith("Heart rate: 92")
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
