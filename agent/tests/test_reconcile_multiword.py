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
