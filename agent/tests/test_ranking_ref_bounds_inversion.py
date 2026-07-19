"""FIX 3 — ranking's referenceRange reader must order an inverted band.

``ranking._ref_bounds`` was a THIRD referenceRange reader that, unlike
``ranges.reference_bounds`` and the ``ReferenceRange`` validator (both fixed in
R3 to swap ``low > high``), returned an inverted structured band verbatim.
``_magnitude`` then scored an out-of-range reading against a backwards band,
diverging the acuity signal from the chart and series views. The fix delegates
``_ref_bounds`` to the single grounded parser so all three readers agree.
"""

from __future__ import annotations

from copilot.rounds.ranges import reference_bounds
from copilot.rounds.ranking import _magnitude, _ref_bounds


def test_ref_bounds_orders_inverted_structured_band() -> None:
    res = {"referenceRange": [{"low": {"value": 5.1}, "high": {"value": 3.5}}]}
    # Now ordered, and identical to the canonical parser.
    assert _ref_bounds(res) == (3.5, 5.1)
    assert _ref_bounds(res) == reference_bounds(res)


def test_ref_bounds_matches_canonical_for_ordered_band() -> None:
    res = {"referenceRange": [{"low": {"value": 3.5}, "high": {"value": 5.1}}]}
    assert _ref_bounds(res) == reference_bounds(res) == (3.5, 5.1)


def test_magnitude_no_longer_diverges_by_band_ordering() -> None:
    # The same reading and the same band, recorded ordered vs inverted, must
    # score the same magnitude. Pre-fix the inverted band gave a different (and
    # wrong) acuity magnitude.
    ordered = {
        "referenceRange": [{"low": {"value": 3.5}, "high": {"value": 5.1}}],
        "valueQuantity": {"value": 5.5},
    }
    inverted = {
        "referenceRange": [{"low": {"value": 5.1}, "high": {"value": 3.5}}],
        "valueQuantity": {"value": 5.5},
    }
    assert _magnitude(inverted) == _magnitude(ordered)
