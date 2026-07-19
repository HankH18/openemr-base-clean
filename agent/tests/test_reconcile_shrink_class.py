"""P1 bite-proof: a value SHORTER than the printed token must not be certified.

The reconciliation gate's coverage check divided only by ``len(value)``
(``matched / len(target)``), so any value whose characters are a subsequence of a
longer, correctly-printed token cleared it at coverage 1.0. The vision model
shrinks ``180`` to ``18``, ``88mcg`` to ``88mg``, ``-2.5`` to ``2.5`` — each rides
in on the *correct* token at value-side coverage 1.0 and similarity >= 0.8, and is
certified ``supported=True`` with that token's bbox. That is a data-integrity
failure: the gate exists to catch invented values, and here it blesses a WRONG one
against the right box (a 10x under-read of a glucose, a sign flip on a base
excess, a 1000x dosing error on a thyroid med).

Coverage is now *two-sided*: the matched span must also account for essentially
all of itself (``matched / len(span) >= _COVERAGE_MIN``), so a span much longer
than the value is rejected. ``18`` vs printed ``180`` has span-side coverage
2/3 = 0.667 and is refused; an equal-length real misread (``·`` read as ``-``)
stays supported.

These tests FAIL on the one-sided gate and PASS once coverage is two-sided.
"""

from __future__ import annotations

import pytest

from copilot.documents.reconcile import reconcile_value


def _one_token(text: str) -> list[dict[str, object]]:
    """A single legible OCR token (conf 0.97) printing ``text`` verbatim."""
    return [{"text": text, "bbox": [0.20, 0.30, 0.08, 0.03], "conf": 0.97}]


class TestShrunkValueIsNotSupported:
    """A value shorter than the token it 'matches' is a different value, not evidence."""

    @pytest.mark.parametrize(
        ("value", "printed"),
        [
            ("18", "180"),  # Glucose 180 -> 18: a 10x under-read
            ("2.5", "-2.5"),  # Base excess -2.5 -> 2.5: a sign flip
            ("88mg", "88mcg"),  # Synthroid 88mcg -> 88mg: a 1000x dosing error
        ],
    )
    def test_value_shorter_than_printed_token_is_unsupported(
        self, value: str, printed: str
    ) -> None:
        # threshold=0.0 is deliberate: the defect is in *location*, not the score.
        result = reconcile_value(value, _one_token(printed), threshold=0.0)

        assert result.supported is False, (
            f"{value!r} is a subsequence of the printed {printed!r}; the one-sided "
            "coverage certified this WRONG value against the right box"
        )
        assert result.bbox is None
        assert result.match_confidence == 0.0

    def test_span_side_coverage_rejects_even_at_zero_threshold(self) -> None:
        """The defect is not a threshold miss: even at threshold 0.0 the shrink is caught.

        Coverage divided only by ``len(value)``: ``18`` vs printed ``180`` covered
        2/2 = 1.0. The span side is 2/3 = 0.667, below ``_COVERAGE_MIN`` — that is
        what rejects it, independent of any confidence threshold.
        """
        result = reconcile_value("18", _one_token("180"), threshold=0.0)

        assert result.supported is False


class TestEqualLengthNoiseStillSupported:
    """Guard the fix does not over-reject: an equal-length real misread stays supported."""

    def test_real_ocr_noise_equal_length_swap_is_kept(self) -> None:
        """The demo page's "·" read as "-": same length, span-coverage ~0.97 — kept.

        The counterpart to the shrink class. Here the value is entirely present and
        only one glyph is wrong, so both coverage sides clear 0.95 and support holds.
        """
        tokens: list[dict[str, object]] = [
            {"text": "Austin,", "bbox": [0.2529, 0.0832, 0.0312, 0.0068], "conf": 0.96},
            {"text": "TX", "bbox": [0.2882, 0.0832, 0.0129, 0.0059], "conf": 0.96},
            {"text": "78701", "bbox": [0.3047, 0.0832, 0.0271, 0.0059], "conf": 0.93},
            {"text": "-", "bbox": [0.3347, 0.0814, 0.0059, 0.0118], "conf": 0.55},
            {"text": "(512)", "bbox": [0.3435, 0.0832, 0.0241, 0.0077], "conf": 0.92},
            {"text": "555-0130", "bbox": [0.3712, 0.0832, 0.0435, 0.0059], "conf": 0.96},
        ]
        result = reconcile_value("Austin, TX 78701 · (512) 555-0130", tokens, threshold=0.0)

        assert result.supported is True
        assert result.bbox is not None
