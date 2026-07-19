"""FIX 3 — an inverted structured reference range must be ordered, not trusted.

`parse_range_text` already swaps a reversed text range (`"145-135"` →
`(135, 145)`), but the structured branch of `reference_bounds` returned
`low.value` / `high.value` verbatim, and `ReferenceRange` admitted `low > high`
unchecked. An inverted structured `referenceRange` then propagated a backwards
band to the drill-down chart and `_distance_to_range`. Two defenses:
`reference_bounds` swaps when both bounds are present and `low > high`, and the
`ReferenceRange` model swaps on construction.
"""

from __future__ import annotations

from copilot.domain.contracts import ReferenceRange
from copilot.rounds.ranges import reference_bounds


class TestReferenceBoundsInversion:
    def test_structured_inverted_two_sided_is_swapped(self) -> None:
        res = {"referenceRange": [{"low": {"value": 5.1}, "high": {"value": 3.5}}]}
        assert reference_bounds(res) == (3.5, 5.1)

    def test_structured_ordered_two_sided_is_untouched(self) -> None:
        res = {"referenceRange": [{"low": {"value": 3.5}, "high": {"value": 5.1}}]}
        assert reference_bounds(res) == (3.5, 5.1)

    def test_structured_one_sided_high_only_is_untouched(self) -> None:
        res = {"referenceRange": [{"high": {"value": 0.04}}]}
        assert reference_bounds(res) == (None, 0.04)

    def test_structured_one_sided_low_only_is_untouched(self) -> None:
        res = {"referenceRange": [{"low": {"value": 10.0}}]}
        assert reference_bounds(res) == (10.0, None)


class TestReferenceRangeModel:
    def test_inverted_bounds_are_swapped_on_construction(self) -> None:
        rr = ReferenceRange(low=5.1, high=3.5)
        assert rr.low == 3.5
        assert rr.high == 5.1

    def test_ordered_bounds_untouched(self) -> None:
        rr = ReferenceRange(low=3.5, high=5.1)
        assert (rr.low, rr.high) == (3.5, 5.1)

    def test_equal_bounds_untouched(self) -> None:
        rr = ReferenceRange(low=4.0, high=4.0)
        assert (rr.low, rr.high) == (4.0, 4.0)

    def test_one_sided_bounds_untouched(self) -> None:
        assert (ReferenceRange(high=0.04).low, ReferenceRange(high=0.04).high) == (None, 0.04)
        assert (ReferenceRange(low=10.0).low, ReferenceRange(low=10.0).high) == (10.0, None)

    def test_none_bounds_untouched(self) -> None:
        assert (ReferenceRange().low, ReferenceRange().high) == (None, None)
