"""feat_ingestion criterion 2 — deterministic PDF rasterization (pypdfium2).

A fixture PDF rasterizes to one image per page with positive pixel width/height
(portrait preserved), and repeat runs are byte-identical. No tesseract, no
network. FROZEN GOAL HARNESS.
"""

from __future__ import annotations

from ._helpers import build_fixture_pdf, page_geometry, raster_pages, resolve_raster


async def test_02_rasterization_deterministic(tmp_path):
    fn = resolve_raster()
    pdf = build_fixture_pdf(("Hemoglobin 13.5 g/dL", "Potassium 4.2 mmol/L"))

    pages1 = await raster_pages(fn, pdf, tmp_path)
    assert len(pages1) == 2, f"the fixture PDF has 2 pages (got {len(pages1)})"

    geo1 = [page_geometry(p) for p in pages1]
    for w, h, img in geo1:
        assert w > 0 and h > 0, "each page must carry positive pixel dimensions"
        assert h > w, "US-letter portrait aspect must be preserved"
        assert isinstance(img, (bytes, bytearray)) and len(img) > 0

    assert geo1[0][2] != geo1[1][2], "pages with different text must render differently"

    pages2 = await raster_pages(fn, pdf, tmp_path)
    geo2 = [page_geometry(p) for p in pages2]
    assert geo1 == geo2, "rasterization must be deterministic (byte-identical re-run)"
