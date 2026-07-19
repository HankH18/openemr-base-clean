"""FIX 2 — a non-finite structured referenceRange bound must be ignored.

``summary._numeric`` already drops a NaN/inf reading value (R3), but its sibling
``ranges._bound_value`` lacked the same ``math.isfinite`` guard. A NaN/inf
``low``/``high`` bound then flowed into the chart band and ``_distance_to_range``,
where — because every comparison with NaN is False — an out-of-range reading is
silently scored in-range, so a real derangement reads as "steady". The bound
reader must drop a non-finite value like a missing one (fail-closed).
"""

from __future__ import annotations

from copilot.rounds.ranges import _bound_value, reference_bounds


def test_nan_low_bound_is_dropped_keeping_finite_high() -> None:
    res = {"referenceRange": [{"low": {"value": float("nan")}, "high": {"value": 5.0}}]}
    assert reference_bounds(res) == (None, 5.0)


def test_inf_high_bound_is_dropped_keeping_finite_low() -> None:
    res = {"referenceRange": [{"low": {"value": 3.5}, "high": {"value": float("inf")}}]}
    assert reference_bounds(res) == (3.5, None)


def test_lone_nan_bound_yields_no_band() -> None:
    # No other bound and no text → a NaN bound must leave nothing derivable.
    res = {"referenceRange": [{"low": {"value": float("nan")}}]}
    assert reference_bounds(res) == (None, None)


def test_bound_value_drops_non_finite() -> None:
    assert _bound_value({"value": float("nan")}) is None
    assert _bound_value({"value": float("inf")}) is None
    assert _bound_value({"value": float("-inf")}) is None
    # Finite values still read through unchanged.
    assert _bound_value({"value": 3.5}) == 3.5
    assert _bound_value({"value": 7}) == 7.0
