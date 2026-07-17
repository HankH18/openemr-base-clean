"""Reconcile an extracted value back to the page's OCR tokens (no invention).

The vision model proposes a value; reconciliation is the deterministic gate that
decides whether that value is actually *on the page*. A value that matches a span
of OCR tokens gets that span's bounding box and a positive match confidence
(``supported=True``); a value found nowhere on the page is flagged
``supported=False`` with no bbox — surfaced as unverified rather than silently
trusted. This is the pixel-level evidence a later grounding pass re-checks.

Matching is span-based because OCR emits one token per *word*: a value like
"Metformin 500 mg PO BID" is never a single token, only a run of adjacent ones.
Scoring against single tokens would report every honest multi-word extraction —
drug + dose + frequency, patient names — as unverified.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any

# Minimum text similarity for a token span to count as the value's source. Exact
# matches score 1.0; this rejects incidental partial overlaps (e.g. a stray "."),
# so "999.9" — absent from the page — matches nothing.
_MATCH_MIN = 0.8

# Widest run of adjacent tokens ever joined into one candidate. Extracted values
# are short (a drug + dose + frequency, a patient name), so this bounds the search
# on a dense page. A value with more words than this reconciles to nothing and is
# surfaced as unverified — the safe direction for a no-invention gate.
_MAX_WINDOW_TOKENS = 12

# A span whose text length falls outside these multiples of the value's length can
# never clear _MATCH_MIN, so it is skipped without running the matcher. ratio() is
# 2*matched/(len_a + len_b) and matched can never exceed the shorter string, so any
# span's score is bounded by 2*min(len_value, len_span)/(len_value + len_span) —
# difflib's own real_quick_ratio. Solving that bound for _MATCH_MIN gives the two
# multiples below. These skip only spans that provably fail, so the winner is
# identical to scoring every span; they are an exact shortcut, not a heuristic.
_MAX_LEN_RATIO = 2.0 / _MATCH_MIN - 1.0
_MIN_LEN_RATIO = _MATCH_MIN / (2.0 - _MATCH_MIN)


@dataclass(frozen=True)
class Reconciliation:
    """Outcome of locating one value in a page's OCR tokens."""

    supported: bool
    bbox: list[float] | None
    match_confidence: float
    page_no: int | None = None


def _token_field(token: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in token:
            return token[name]
    raise KeyError(f"OCR token is missing all of {names!r}")


def _normalize(text: str) -> str:
    return text.strip().lower()


def _union_bbox(boxes: Sequence[Sequence[float]]) -> list[float]:
    """Smallest ``[x, y, w, h]`` covering every box in a winning span."""
    if len(boxes) == 1:
        # Returned verbatim: recomputing w as (x + w) - x would perturb a
        # single-token bbox in the last float digit.
        return [float(v) for v in boxes[0]]
    x0 = min(float(box[0]) for box in boxes)
    y0 = min(float(box[1]) for box in boxes)
    x1 = max(float(box[0]) + float(box[2]) for box in boxes)
    y1 = max(float(box[1]) + float(box[3]) for box in boxes)
    return [x0, y0, x1 - x0, y1 - y0]


def reconcile_value(
    value: str,
    tokens: Sequence[Mapping[str, Any]],
    page_no: int = 1,
    threshold: float = 0.0,
) -> Reconciliation:
    """Locate ``value`` among ``tokens``; return its bbox + confidence, or flag it.

    Scores ``value`` against every span of 1..N adjacent tokens (N = the value's
    word count, capped at :data:`_MAX_WINDOW_TOKENS`) and returns the union bbox of
    the best-scoring span. ``tokens`` must be in reading order — that order is what
    makes a span contiguous on the page — which both OCR engines emit.

    ``threshold`` is the minimum match confidence to count as supported (the
    pipeline passes ``Settings.doc_extraction_confidence_threshold``; the default
    0.0 means "any real token match is enough").
    """
    target = _normalize(value)
    best_score = 0.0
    best_span: tuple[int, int] | None = None
    if target:
        # Parsed once up front: the loop below revisits each token in up to
        # _MAX_WINDOW_TOKENS different spans.
        texts = [_normalize(str(_token_field(token, "text", "word"))) for token in tokens]
        confs = [float(_token_field(token, "conf", "confidence")) for token in tokens]
        max_window = min(len(target.split()), _MAX_WINDOW_TOKENS)
        max_span_len = len(target) * _MAX_LEN_RATIO
        min_span_len = len(target) * _MIN_LEN_RATIO
        for start in range(len(texts)):
            span_text = ""
            span_conf = 1.0
            for end in range(start, min(start + max_window, len(texts))):
                span_text = texts[end] if end == start else f"{span_text} {texts[end]}"
                # A span is only as trustworthy as its least legible word, so the
                # weakest token governs — never an average that could hide one.
                span_conf = min(span_conf, confs[end])
                if len(span_text) > max_span_len:
                    break  # every wider span is longer still — see _MAX_LEN_RATIO
                if len(span_text) < min_span_len:
                    continue  # too short to match, but widening may fix that
                similarity = SequenceMatcher(None, target, span_text).ratio()
                if similarity < _MATCH_MIN:
                    continue
                score = similarity * span_conf
                if score > best_score:
                    best_score = score
                    best_span = (start, end)
    if best_span is not None and best_score >= threshold:
        boxes = [
            [float(v) for v in _token_field(tokens[i], "bbox", "box")]
            for i in range(best_span[0], best_span[1] + 1)
        ]
        return Reconciliation(
            supported=True,
            bbox=_union_bbox(boxes),
            match_confidence=best_score,
            page_no=page_no,
        )
    return Reconciliation(supported=False, bbox=None, match_confidence=0.0, page_no=page_no)
