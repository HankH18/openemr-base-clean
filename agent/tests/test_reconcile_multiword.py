"""Direct tests for ``reconcile_value`` against word-level OCR tokens.

OCR emits one token per word, so a real extracted value ("Metformin 500 mg PO
BID", a patient's full name) only exists on the page as a run of adjacent tokens.
These tests drive ``reconcile_value`` directly — the stub fixtures are all
single-token by construction, so the pipeline tests never exercised a multi-word
value and the span matching went untested.

The load-bearing case is :meth:`TestNoInventionGate.test_value_absent_from_page`:
span matching must widen what reconciles *without* softening the no-invention
gate, so a value that is not on the page still comes back unsupported.
"""

from __future__ import annotations

import pytest

from copilot.documents.reconcile import reconcile_value

# A word-level OCR page in reading order, mirroring what TesseractOcr emits:
# normalized [x, y, w, h] boxes in [0, 1], confidences in [0, 1].
PAGE_TOKENS: list[dict[str, object]] = [
    {"text": "Patient", "bbox": [0.10, 0.05, 0.10, 0.03], "conf": 0.98},
    {"text": "Name:", "bbox": [0.21, 0.05, 0.07, 0.03], "conf": 0.98},
    {"text": "Marisol", "bbox": [0.30, 0.05, 0.09, 0.03], "conf": 0.97},
    {"text": "Quintanilla", "bbox": [0.40, 0.05, 0.13, 0.03], "conf": 0.96},
    {"text": "Metformin", "bbox": [0.10, 0.25, 0.16, 0.03], "conf": 0.97},
    {"text": "500", "bbox": [0.28, 0.25, 0.05, 0.03], "conf": 0.96},
    {"text": "mg", "bbox": [0.35, 0.25, 0.04, 0.03], "conf": 0.95},
    {"text": "PO", "bbox": [0.41, 0.25, 0.04, 0.03], "conf": 0.95},
    {"text": "BID", "bbox": [0.47, 0.25, 0.05, 0.03], "conf": 0.94},
    {"text": "Hemoglobin", "bbox": [0.10, 0.40, 0.20, 0.03], "conf": 0.98},
    {"text": "13.5", "bbox": [0.32, 0.40, 0.06, 0.03], "conf": 0.97},
]


def _token(text: str) -> dict[str, object]:
    """The one page token with this verbatim text."""
    return next(token for token in PAGE_TOKENS if token["text"] == text)


def _covers(bbox: list[float], inner: list[float]) -> bool:
    """Does ``bbox`` fully contain ``inner``? Both are ``[x, y, w, h]``."""
    bx, by, bw, bh = bbox
    ix, iy, iw, ih = inner
    return (
        bx <= ix + 1e-9
        and by <= iy + 1e-9
        and bx + bw >= ix + iw - 1e-9
        and by + bh >= iy + ih - 1e-9
    )


class TestMultiWordValues:
    """A value spanning several tokens reconciles to the span's union bbox."""

    @pytest.mark.parametrize(
        "value",
        [
            "Marisol Quintanilla",
            "Metformin 500 mg PO BID",
            "Hemoglobin 13.5",
            "500 mg PO",  # a span starting mid-line
        ],
    )
    def test_multi_word_value_is_supported(self, value: str) -> None:
        result = reconcile_value(value, PAGE_TOKENS)

        assert result.supported is True
        assert result.bbox is not None
        assert result.match_confidence > 0.0

    @pytest.mark.parametrize(
        ("value", "words"),
        [
            ("Marisol Quintanilla", ["Marisol", "Quintanilla"]),
            ("Metformin 500 mg PO BID", ["Metformin", "500", "mg", "PO", "BID"]),
            ("500 mg PO", ["500", "mg", "PO"]),
        ],
    )
    def test_union_bbox_covers_every_constituent_token(
        self, value: str, words: list[str]
    ) -> None:
        """The overlay box must actually sit over the words it claims as evidence."""
        result = reconcile_value(value, PAGE_TOKENS)

        assert result.bbox is not None
        for word in words:
            inner = [float(v) for v in _token(word)["bbox"]]  # type: ignore[union-attr]
            assert _covers(result.bbox, inner), f"union bbox misses {word!r}"

    def test_union_bbox_is_the_tight_envelope(self) -> None:
        """Tight, not merely covering — a page-sized box would 'cover' too."""
        result = reconcile_value("Marisol Quintanilla", PAGE_TOKENS)

        # "Marisol" starts at x=0.30; "Quintanilla" ends at 0.40 + 0.13 = 0.53.
        assert result.bbox is not None
        x, y, w, h = result.bbox
        assert x == pytest.approx(0.30)
        assert y == pytest.approx(0.05)
        assert w == pytest.approx(0.23)
        assert h == pytest.approx(0.03)

    def test_span_confidence_is_governed_by_weakest_token(self) -> None:
        """An exact match scores the least legible word's confidence, not an average."""
        result = reconcile_value("Metformin 500 mg PO BID", PAGE_TOKENS)

        # Exact text match (similarity 1.0) x min conf across the span ("BID", 0.94).
        assert result.match_confidence == pytest.approx(0.94)


class TestNoInventionGate:
    """Widening what matches must not soften the gate: absent values stay unsupported."""

    @pytest.mark.parametrize(
        "value",
        [
            "Shortness of breath",  # nowhere on the page
            "Lisinopril 10 mg daily",  # plausible-looking, but a different drug
            "Marisol Featherstonehaugh",  # right first word, invented surname
            "999.9",  # numeric value absent from the page
            "Metformin 500 mg PO BID once daily with food",  # over-extended tail
        ],
    )
    def test_value_absent_from_page(self, value: str) -> None:
        result = reconcile_value(value, PAGE_TOKENS)

        assert result.supported is False
        assert result.bbox is None
        assert result.match_confidence == 0.0

    def test_non_adjacent_words_do_not_match(self) -> None:
        """A span is contiguous: words scattered across the page are not evidence.

        "Patient" and "Metformin" are both on the page but far apart, so the phrase
        "Patient Metformin" was never printed on it.
        """
        result = reconcile_value("Patient Metformin", PAGE_TOKENS)

        assert result.supported is False
        assert result.bbox is None

    def test_empty_page_supports_nothing(self) -> None:
        result = reconcile_value("Metformin 500 mg PO BID", [])

        assert result.supported is False
        assert result.bbox is None

    @pytest.mark.parametrize("value", ["", "   "])
    def test_empty_value_is_unsupported(self, value: str) -> None:
        result = reconcile_value(value, PAGE_TOKENS)

        assert result.supported is False
        assert result.bbox is None

    def test_threshold_withholds_support_below_the_bar(self) -> None:
        """A located span still fails the gate when it cannot clear the threshold."""
        result = reconcile_value("Metformin 500 mg PO BID", PAGE_TOKENS, threshold=0.99)

        assert result.supported is False
        assert result.bbox is None

    @pytest.mark.parametrize(
        "value",
        [
            "Marisol Quintanilla",
            "Shortness of breath",
            "Metformin",
            "999.9",
            "",
        ],
    )
    def test_supported_implies_a_bbox(self, value: str) -> None:
        """The invariant the overlay depends on: support is never boxless."""
        result = reconcile_value(value, PAGE_TOKENS)

        assert result.supported is (result.bbox is not None)


class TestSingleTokenValues:
    """The pre-existing single-token path must behave exactly as before."""

    @pytest.mark.parametrize(
        ("value", "word"),
        [
            ("Metformin", "Metformin"),
            ("13.5", "13.5"),
            ("Hemoglobin", "Hemoglobin"),
            ("Quintanilla", "Quintanilla"),
        ],
    )
    def test_single_token_value_matches(self, value: str, word: str) -> None:
        result = reconcile_value(value, PAGE_TOKENS)

        assert result.supported is True
        assert result.bbox == _token(word)["bbox"]
        assert result.match_confidence == pytest.approx(float(_token(word)["conf"]))  # type: ignore[arg-type]

    def test_single_token_value_is_not_widened_to_a_span(self) -> None:
        """"Metformin" must box the drug name alone, not swallow its dose."""
        result = reconcile_value("Metformin", PAGE_TOKENS)

        assert result.bbox == [0.10, 0.25, 0.16, 0.03]

    def test_page_no_is_carried_through(self) -> None:
        assert reconcile_value("Metformin", PAGE_TOKENS, page_no=3).page_no == 3
        assert reconcile_value("absent", PAGE_TOKENS, page_no=3).page_no == 3

    def test_alternate_token_field_names(self) -> None:
        """The word/box/confidence aliases work for spans, as they did for tokens."""
        tokens: list[dict[str, object]] = [
            {"word": "Marisol", "box": [0.30, 0.05, 0.09, 0.03], "confidence": 0.97},
            {"word": "Quintanilla", "box": [0.40, 0.05, 0.13, 0.03], "confidence": 0.96},
        ]
        result = reconcile_value("Marisol Quintanilla", tokens)

        assert result.supported is True
        assert result.bbox is not None
        assert result.bbox[0] == pytest.approx(0.30)


# One row of a medication table, in the row-wise reading order Tesseract actually
# emits, traced from the real sample_medication_list.pdf (612x792pt, ~9.6pt type
# on ~11pt line pitch). Two of its cells wrap, and both wraps are measured from
# that page rather than invented:
#
#     40 mg   PO   QHS (once daily,     Type 2 diabetes   06/28/2026
#                  bedtime)             mellitus
#
# Reading order runs across the whole row, so each wrapped tail is reached only
# *after* the row's remaining columns — "bedtime)" lands at index 10, five tokens
# past the "daily," it continues. That is the whole difficulty: contiguity in the
# stream is not contiguity on the page.
#
# Boxes are normalized [x, y, w, h]; every token is 0.010 tall (the page's ~7.2pt
# glyphs) and the wrap drops 0.0138 (~11pt, 1.38 text-heights). The geometry that
# decides everything, in text-heights (tolerance is 0.5 = 0.005):
#   "QHS"  x=0.5725 -> "bedtime)" x=0.5710   delta 0.15 th   (a wrap: 3.3x inside)
#   "Type" x=0.7603 -> "mellitus" x=0.7590   delta 0.13 th   (a wrap: 3.8x inside)
#   "QHS"  x=0.5725 -> "mellitus" x=0.7590   delta 18.6 th   (a column: 37x outside)
# Within a cell, words sit ~0.3 text-heights apart; the nearest column boundary is
# 5.9 away. Nothing here is near a threshold.
TABLE_TOKENS: list[dict[str, object]] = [
    {"text": "40", "bbox": [0.3732, 0.4483, 0.018, 0.010], "conf": 0.96},
    {"text": "mg", "bbox": [0.3950, 0.4483, 0.020, 0.010], "conf": 0.95},
    {"text": "PO", "bbox": [0.4739, 0.4483, 0.020, 0.010], "conf": 0.94},
    {"text": "QHS", "bbox": [0.5725, 0.4483, 0.026, 0.010], "conf": 0.93},
    {"text": "(once", "bbox": [0.6015, 0.4483, 0.032, 0.010], "conf": 0.92},
    {"text": "daily,", "bbox": [0.6365, 0.4483, 0.031, 0.010], "conf": 0.91},
    {"text": "Type", "bbox": [0.7603, 0.4483, 0.031, 0.010], "conf": 0.97},
    {"text": "2", "bbox": [0.7943, 0.4483, 0.008, 0.010], "conf": 0.96},
    {"text": "diabetes", "bbox": [0.8053, 0.4483, 0.059, 0.010], "conf": 0.95},
    {"text": "06/28/2026", "bbox": [0.9300, 0.4483, 0.065, 0.010], "conf": 0.96},
    {"text": "bedtime)", "bbox": [0.5710, 0.4621, 0.055, 0.010], "conf": 0.90},
    {"text": "mellitus", "bbox": [0.7590, 0.4621, 0.059, 0.010], "conf": 0.94},
]


def _table_token(text: str) -> dict[str, object]:
    """The one table token with this verbatim text."""
    return next(token for token in TABLE_TOKENS if token["text"] == text)


class TestWrappedCellValues:
    """A value that wraps inside a table cell is still on the page — and matches.

    Reading order is row-wise, so the tail of a wrapped cell is not stream-adjacent
    to its head: between "daily," and "bedtime)" sit every remaining column of the
    row. Contiguity alone therefore reports a verbatim value as unverified. The
    geometry is what identifies the tail: it drops one line-height and returns to
    the cell's left edge.
    """

    def test_wrapped_cell_value_is_supported(self) -> None:
        result = reconcile_value("QHS (once daily, bedtime)", TABLE_TOKENS)

        assert result.supported is True
        assert result.bbox is not None
        # Exact text match, so the score is the span's least legible word —
        # "bedtime)" at 0.90, which is on the *wrapped* line: proof the tail is
        # genuinely part of the span rather than a prefix match that stopped early.
        assert result.match_confidence == pytest.approx(0.90)

    def test_wrapped_union_bbox_covers_both_lines(self) -> None:
        """The overlay must sit over every word it claims — across the wrap."""
        result = reconcile_value("QHS (once daily, bedtime)", TABLE_TOKENS)

        assert result.bbox is not None
        for word in ["QHS", "(once", "daily,", "bedtime)"]:
            inner = [float(v) for v in _table_token(word)["bbox"]]  # type: ignore[union-attr]
            assert _covers(result.bbox, inner), f"union bbox misses {word!r}"

    def test_wrapped_union_bbox_spans_two_lines(self) -> None:
        """A two-line box, not a one-line box: the tail is really enclosed."""
        result = reconcile_value("QHS (once daily, bedtime)", TABLE_TOKENS)

        assert result.bbox is not None
        x, y, w, h = result.bbox
        # Left edge is the wrapped tail's (0.5710), which sits left of "QHS".
        assert x == pytest.approx(0.5710)
        assert y == pytest.approx(0.4483)
        # Top of line one (0.4483) to the bottom of line two (0.4621 + 0.010).
        assert h == pytest.approx(0.0238)
        # ...and it stops well short of the neighbouring column at x=0.7603.
        assert x + w < 0.7603

    def test_wrapped_match_does_not_swallow_the_next_column(self) -> None:
        """The row's other cells are not evidence for this one.

        Regression guard: continuing the stream after the wrap token walks
        straight out of the cell and into the *next* column's wrapped tail
        ("mellitus"), which once produced a box spanning both.
        """
        result = reconcile_value("QHS (once daily, bedtime)", TABLE_TOKENS)

        assert result.bbox is not None
        for word in ["Type", "2", "diabetes", "06/28/2026", "mellitus"]:
            inner = [float(v) for v in _table_token(word)["bbox"]]  # type: ignore[union-attr]
            assert not _covers(result.bbox, inner), f"union bbox swallowed {word!r}"

    def test_a_second_independent_cell_wraps_by_the_same_rule(self) -> None:
        """The indication cell wraps too — same page, different column, no retuning.

        "Type 2 diabetes mellitus" is a wrap of a different cell at a different x,
        whose tail is nine tokens downstream of its head. That one geometric rule
        locates both it and the frequency cell is the evidence the rule describes
        wrapping in general, rather than having been fitted to one example.
        """
        result = reconcile_value("Type 2 diabetes mellitus", TABLE_TOKENS)

        assert result.supported is True
        assert result.bbox is not None
        # Scored by its weakest word — "mellitus", the wrapped tail itself.
        assert result.match_confidence == pytest.approx(0.94)
        x, _y, _w, h = result.bbox
        assert x == pytest.approx(0.7590)
        assert h == pytest.approx(0.0238)  # two lines
        for word in ["Type", "2", "diabetes", "mellitus"]:
            inner = [float(v) for v in _table_token(word)["bbox"]]  # type: ignore[union-attr]
            assert _covers(result.bbox, inner), f"union bbox misses {word!r}"


class TestWrapGeometryIsScaleInvariant:
    """The same page at another DPI must reconcile identically.

    Tesseract reports pixel boxes, whose magnitude depends entirely on the render
    resolution; ``TesseractOcr`` then normalizes them to [0, 1]. A tolerance
    written as a pixel constant would be silently wrong at any other DPI — and
    "wrong" here means either inventing evidence or losing it. Deriving every
    tolerance from the page's own median token height is what makes the rule a
    property of the *layout* rather than of the render, so this asserts the
    decisions survive a 1275x1650 (150dpi Letter) rescale unchanged.
    """

    @staticmethod
    def _rescale(page_w: float, page_h: float) -> list[dict[str, object]]:
        return [
            {
                "text": token["text"],
                "conf": token["conf"],
                "bbox": [
                    float(token["bbox"][0]) * page_w,  # type: ignore[index]
                    float(token["bbox"][1]) * page_h,  # type: ignore[index]
                    float(token["bbox"][2]) * page_w,  # type: ignore[index]
                    float(token["bbox"][3]) * page_h,  # type: ignore[index]
                ],
            }
            for token in TABLE_TOKENS
        ]

    @pytest.mark.parametrize(
        ("value", "supported"),
        [
            ("QHS (once daily, bedtime)", True),  # the wrap still resolves...
            ("Type 2 diabetes mellitus", True),
            ("diabetes bedtime)", False),  # ...and the column guard still holds
            ("QHS (once daily, mellitus", False),
        ],
    )
    def test_pixel_space_page_decides_the_same(self, value: str, supported: bool) -> None:
        pixels = self._rescale(1275.0, 1650.0)

        assert reconcile_value(value, pixels).supported is supported

    def test_wrapped_bbox_is_the_same_box_in_pixel_space(self) -> None:
        """Not just the same verdict — the same rectangle, rescaled."""
        normalized = reconcile_value("QHS (once daily, bedtime)", TABLE_TOKENS)
        pixels = reconcile_value("QHS (once daily, bedtime)", self._rescale(1275.0, 1650.0))

        assert normalized.bbox is not None
        assert pixels.bbox is not None
        assert pixels.bbox[0] == pytest.approx(normalized.bbox[0] * 1275.0)
        assert pixels.bbox[3] == pytest.approx(normalized.bbox[3] * 1650.0)


class TestWrapIsGeometricNotProximity:
    """Continuing a cell is not the same as "anything nearby continues it"."""

    def test_continuation_in_a_different_column_does_not_match(self) -> None:
        """"diabetes bedtime)" was never printed on this page.

        Both words are on the page, one line apart — but "bedtime)" continues the
        *frequency* cell, not the indication cell that "diabetes" ends. They are
        also not stream-adjacent (indices 8 and 10), so contiguity cannot join
        them either. Only a wrap rule that had decayed into "match anything
        nearby" would call this supported, and doing so would invent a phrase —
        precisely what the gate exists to prevent.
        """
        result = reconcile_value("diabetes bedtime)", TABLE_TOKENS)

        assert result.supported is False
        assert result.bbox is None
        assert result.match_confidence == 0.0

    def test_wrap_of_a_different_cell_does_not_extend_this_one(self) -> None:
        """"mellitus" wraps the indication cell; it cannot tail the frequency cell.

        Guards the subtler direction: here the wrap to "bedtime)" is legitimate,
        and the flaw is a chain that keeps walking the wrapped *line* into the
        neighbouring cell's tail. The value reads plausibly, and every word of it
        appears somewhere on the row — which is exactly why it must not match.
        """
        result = reconcile_value("QHS (once daily, mellitus", TABLE_TOKENS)

        assert result.supported is False
        assert result.bbox is None

    def test_wrapped_tail_is_not_borrowed_across_columns(self) -> None:
        """The indication cell cannot borrow the frequency cell's wrapped tail."""
        result = reconcile_value("Type 2 diabetes bedtime)", TABLE_TOKENS)

        assert result.supported is False
        assert result.bbox is None

    @pytest.mark.parametrize(
        "value",
        [
            "Rivaroxaban 20 mg",  # a drug that is not in this table
            "QAM (once daily, morning)",  # the wrapped cell's shape, different words
            "Hypothyroidism",  # a plausible indication, absent from the page
        ],
    )
    def test_absent_value_stays_unsupported(self, value: str) -> None:
        """Widening to wrapped cells must not soften the no-invention gate."""
        result = reconcile_value(value, TABLE_TOKENS)

        assert result.supported is False
        assert result.bbox is None
        assert result.match_confidence == 0.0


class TestTableRowContiguousBehaviorUnchanged:
    """Single-token and single-line matching on a table page behave as before."""

    def test_single_token_value_keeps_its_verbatim_bbox(self) -> None:
        result = reconcile_value("diabetes", TABLE_TOKENS)

        assert result.supported is True
        assert result.bbox == _table_token("diabetes")["bbox"]
        assert result.match_confidence == pytest.approx(0.95)

    def test_single_line_span_stays_on_one_line(self) -> None:
        """A value that does not wrap gets a one-line box, not a wrapped one."""
        result = reconcile_value("40 mg PO", TABLE_TOKENS)

        assert result.supported is True
        assert result.bbox is not None
        x, y, _w, h = result.bbox
        assert y == pytest.approx(0.4483)
        assert h == pytest.approx(0.010)  # one token tall — no wrap was taken
        assert x == pytest.approx(0.3732)

    def test_supported_implies_a_bbox_on_a_table_page(self) -> None:
        for value in ["QHS (once daily, bedtime)", "diabetes bedtime)", "40 mg PO", ""]:
            result = reconcile_value(value, TABLE_TOKENS)

            assert result.supported is (result.bbox is not None)
