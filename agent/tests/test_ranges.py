"""Grounded reference-range parser + the closed standard-vitals table."""

from __future__ import annotations

from typing import Any

import pytest

from copilot.rounds.ranges import (
    VITALS_RANGES,
    parse_range_text,
    reference_bounds,
    vitals_range,
)


class TestParseRangeText:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("135-145", (135.0, 145.0)),
            ("0.5-2.2", (0.5, 2.2)),
            ("3.5 - 5.1", (3.5, 5.1)),
            ("12 to 20", (12.0, 20.0)),
            ("90" + chr(0x2013) + "120", (90.0, 120.0)),  # en-dash separator
            ("145-135", (135.0, 145.0)),  # reversed order tolerated
        ],
    )
    def test_two_sided(self, text: str, expected: tuple[float, float]) -> None:
        assert parse_range_text(text) == expected

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("<0.04", (None, 0.04)),  # the troponin case that used to yield no band
            ("<=0.04", (None, 0.04)),
            ("≤ 0.04", (None, 0.04)),
            (">10", (10.0, None)),
            (">=2.5", (2.5, None)),
            ("≥ 2.5", (2.5, None)),
        ],
    )
    def test_one_sided(self, text: str, expected: tuple[float | None, float | None]) -> None:
        assert parse_range_text(text) == expected

    @pytest.mark.parametrize("text", ["", "   ", "n/a", "normal", "5", "abc", "<abc"])
    def test_unparseable_yields_no_band(self, text: str) -> None:
        assert parse_range_text(text) == (None, None)


class TestReferenceBounds:
    def test_structured_two_sided(self) -> None:
        res = {"referenceRange": [{"low": {"value": 3.5}, "high": {"value": 5.1}}]}
        assert reference_bounds(res) == (3.5, 5.1)

    def test_structured_one_sided_high_only(self) -> None:
        res = {"referenceRange": [{"high": {"value": 0.04}}]}
        assert reference_bounds(res) == (None, 0.04)

    def test_text_fallback_one_sided(self) -> None:
        # No structured bounds → parse the free-text form (real-OpenEMR shape).
        res = {"referenceRange": [{"text": "<0.04"}]}
        assert reference_bounds(res) == (None, 0.04)

    def test_structured_wins_over_text(self) -> None:
        res = {"referenceRange": [{"low": {"value": 135.0}, "high": {"value": 145.0}, "text": "junk"}]}
        assert reference_bounds(res) == (135.0, 145.0)

    @pytest.mark.parametrize(
        "res",
        [
            {},
            {"referenceRange": []},
            {"referenceRange": [{"text": "n/a"}]},
            {"referenceRange": ["not-a-mapping"]},
        ],
    )
    def test_no_band(self, res: dict[str, Any]) -> None:
        assert reference_bounds(res) == (None, None)


class TestVitalsRange:
    @pytest.mark.parametrize(
        ("label", "expected"),
        [
            ("Heart Rate", (60.0, 100.0)),
            ("heart rate", (60.0, 100.0)),
            ("Respiratory Rate", (12.0, 20.0)),
            ("Oxygen Saturation", (95.0, 100.0)),
            ("SpO2", (95.0, 100.0)),
            ("Systolic Blood Pressure", (90.0, 120.0)),
            ("Diastolic Blood Pressure", (60.0, 80.0)),
        ],
    )
    def test_known_vitals(self, label: str, expected: tuple[float, float]) -> None:
        assert vitals_range(label) == expected

    @pytest.mark.parametrize(
        ("unit", "expected"),
        [
            ("°C", (36.1, 37.2)),
            ("Cel", (36.1, 37.2)),
            ("°F", (97.0, 99.0)),
            ("degF", (97.0, 99.0)),
        ],
    )
    def test_temperature_is_unit_aware(
        self, unit: str, expected: tuple[float, float]
    ) -> None:
        assert vitals_range("Body Temperature", unit) == expected

    def test_temperature_unknown_unit_is_neutral(self) -> None:
        # A temperature without a recognizable unit must not guess a band.
        assert vitals_range("Temperature", "") == (None, None)
        assert vitals_range("Temperature", "K") == (None, None)

    def test_unknown_metric_is_neutral(self) -> None:
        assert vitals_range("Troponin I") == (None, None)
        assert vitals_range("Potassium", "mmol/L") == (None, None)

    def test_table_is_a_small_closed_standard(self) -> None:
        # A documented clinical baseline, not a sprawling population table.
        assert len(VITALS_RANGES) <= 10
        assert all(low < high for low, high in VITALS_RANGES.values())
