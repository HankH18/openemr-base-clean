"""FIX 2 — a non-finite `valueQuantity.value` must not crash the summary.

JSON forbids `NaN`/`Infinity`, but `json.loads` / `httpx` parse them if a
server emits them. `_numeric` accepted any `int|float`, so a non-finite value
flowed into `_fmt_num`'s `int(x)` — `int(nan)` raises `ValueError`, `int(inf)`
raises `OverflowError` — taking down `build_summary_claims`. The value guard
must add a finiteness check so a non-finite reading drops like a non-numeric
one (fail-closed, no crash).
"""

from __future__ import annotations

import math
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


def test_nan_prior_does_not_crash_and_drops_from_trend() -> None:
    # Latest is finite; the prior reading is NaN. Before the fix the NaN survived
    # `_numeric`, made `_trusted_pair` return a (finite, NaN) pair, and blew up in
    # `int(abs(nan))`. After the fix the NaN reading drops → no delta, no crash.
    resources = [
        _obs("hr-2", "Heart rate", 92, "2026-07-11T05:00:00Z"),  # latest, finite
        _obs("hr-1", "Heart rate", float("nan"), "2026-07-10T05:00:00Z"),  # prior, NaN
    ]
    claims = build_summary_claims(resources)
    assert len(claims) == 1
    text = claims[0].text
    assert text.startswith("Heart Rate: 92")
    # No fabricated up/down delta computed against a non-finite operand.
    assert "↑" not in text and "↓" not in text


def test_inf_prior_does_not_crash_and_drops_from_trend() -> None:
    resources = [
        _obs("g-2", "Glucose", 90, "2026-07-11T05:00:00Z", unit="mg/dL"),  # latest
        _obs("g-1", "Glucose", float("inf"), "2026-07-10T05:00:00Z", unit="mg/dL"),  # prior, Inf
    ]
    claims = build_summary_claims(resources)
    assert len(claims) == 1
    text = claims[0].text
    assert text.startswith("Glucose: 90 mg/dL")
    assert "↑" not in text and "↓" not in text


def test_nan_latest_single_reading_does_not_crash() -> None:
    # A lone non-finite reading must not raise; the metric row is still produced.
    resources = [_obs("k-1", "Potassium", float("nan"), "2026-07-10T05:00:00Z", unit="mEq/L")]
    claims = build_summary_claims(resources)
    assert len(claims) == 1


def test_finite_readings_still_produce_a_trend() -> None:
    # Guard must not disturb the ordinary finite path.
    assert math.isfinite(104.0)
    resources = [
        _obs("hr-2", "Heart rate", 104.0, "2026-07-09T05:00:00Z"),  # prior
        _obs("hr-1", "Heart rate", 92.0, "2026-07-10T05:00:00Z"),  # latest
    ]
    text = build_summary_claims(resources)[0].text
    assert "↓12" in text
