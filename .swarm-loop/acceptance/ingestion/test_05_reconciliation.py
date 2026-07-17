"""feat_ingestion criterion 5 — OCR reconciliation (no invention).

A value present in the OCR tokens reconciles to a bbox that locates the value
token plus a positive match confidence; a value absent from the page persists
supported=False (flagged, never silently trusted). FROZEN GOAL HARNESS.
"""

from __future__ import annotations

from ._helpers import (
    OCR_TOKENS,
    field,
    opt_field,
    reconcile_one,
    rects_intersect,
    resolve_reconcile,
)


async def test_05_reconciliation(tmp_path):
    fn = resolve_reconcile()

    # Matched: "13.5" is on the page.
    matched = await reconcile_one(fn, "13.5", OCR_TOKENS, tmp_path)
    supported = field(matched, "supported", "matched", "found", "ok", what="reconcile result")
    assert bool(supported) is True, "a value present in the OCR tokens must be supported"

    bbox = field(matched, "bbox", "box", what="reconcile result")
    assert bbox is not None and len(list(bbox)) == 4, "a matched value carries a [x,y,w,h] bbox"
    coords = [float(v) for v in bbox]
    assert all(0.0 <= c <= 1.0 for c in coords), "bbox must be normalized to [0, 1]"
    value_token_bbox = [0.32, 0.10, 0.06, 0.03]  # the "13.5" token in OCR_TOKENS
    assert rects_intersect(coords, value_token_bbox), (
        f"the reconciled bbox {coords} must locate the value token at {value_token_bbox}"
    )

    conf = field(matched, "match_confidence", "confidence", "conf", what="reconcile result")
    assert 0.0 < float(conf) <= 1.0, "a matched value carries a positive match confidence"

    # Unmatched: "999.9" is nowhere on the page — flagged, not invented.
    unmatched = await reconcile_one(fn, "999.9", OCR_TOKENS, tmp_path)
    un_supported = field(unmatched, "supported", "matched", "found", "ok", what="reconcile result")
    assert bool(un_supported) is False, (
        "a value absent from the OCR tokens must persist supported=False"
    )
    un_bbox = opt_field(unmatched, "bbox", "box", default=None)
    assert not un_bbox, "an unmatched value must not be given a bbox"
