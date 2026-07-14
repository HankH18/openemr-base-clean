"""Reconcile an extracted value back to the page's OCR tokens (no invention).

The vision model proposes a value; reconciliation is the deterministic gate that
decides whether that value is actually *on the page*. A value that matches an OCR
token gets the token's bounding box and a positive match confidence
(``supported=True``); a value found nowhere on the page is flagged
``supported=False`` with no bbox — surfaced as unverified rather than silently
trusted. This is the pixel-level evidence a later grounding pass re-checks.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any

# Minimum text similarity for a token to count as the value's source. Exact
# matches score 1.0; this rejects incidental partial overlaps (e.g. a stray "."),
# so "999.9" — absent from the page — matches nothing.
_MATCH_MIN = 0.8


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


def reconcile_value(
    value: str,
    tokens: Sequence[Mapping[str, Any]],
    page_no: int = 1,
    threshold: float = 0.0,
) -> Reconciliation:
    """Locate ``value`` among ``tokens``; return its bbox + confidence, or flag it.

    ``threshold`` is the minimum match confidence to count as supported (the
    pipeline passes ``Settings.doc_extraction_confidence_threshold``; the default
    0.0 means "any real token match is enough").
    """
    target = _normalize(value)
    best_score = 0.0
    best_bbox: list[float] | None = None
    if target:
        for token in tokens:
            text = _normalize(str(_token_field(token, "text", "word")))
            confidence = float(_token_field(token, "conf", "confidence"))
            similarity = SequenceMatcher(None, target, text).ratio()
            if similarity < _MATCH_MIN:
                continue
            score = similarity * confidence
            if score > best_score:
                best_score = score
                best_bbox = [float(v) for v in _token_field(token, "bbox", "box")]
    if best_bbox is not None and best_score >= threshold:
        return Reconciliation(
            supported=True, bbox=best_bbox, match_confidence=best_score, page_no=page_no
        )
    return Reconciliation(supported=False, bbox=None, match_confidence=0.0, page_no=page_no)
