"""Deterministic PDF rasterization (pypdfium2).

Renders every page of a PDF to a PNG at a fixed DPI so downstream OCR + vision
extraction see the same pixels every run. Pure and offline — no tesseract, no
network — and byte-deterministic: the same PDF at the same DPI renders to
byte-identical PNGs, which is what the append-only extraction store relies on to
keep a re-ingest reproducible.

Rasterization failure (an unreadable / non-PDF byte string) raises
:class:`RasterizationError` so the pipeline can fail the ingestion closed rather
than persist a half-built document.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

import pypdfium2 as pdfium  # type: ignore[import-untyped]  # ships no py.typed marker

# PDF user-space units are points; 72 points == 1 inch.
_POINTS_PER_INCH = 72.0

# Fallback render DPI when a caller does not pass one (the pipeline passes
# ``Settings.ocr_dpi``).
DEFAULT_DPI = 200

# --- resource caps ----------------------------------------------------------
#
# A PDF page's MediaBox is attacker-/accident-controlled and costs the uploader
# NOTHING: the page geometry is a handful of bytes in the file, but the RENDER is
# quadratic in it. A 544-byte PDF declaring a 60x60in MediaBox renders to
# 12000x12000 (144 MP, ~1.1 GB peak RSS) at the deployed default ocr_dpi=200; a
# 546-byte 200x200in page (legal per the PDF spec, which permits up to 200in)
# renders to 40000x40000 = 1.6 gigapixel and asks for 6.4 GB of RGBA. The droplet
# is 1 vCPU / 2 GB with no mem_limit on the agent container, so either one
# OOM-kills the process and takes chat + rounds + document reads down with it.
# This is not primarily an attack: a large-format scan (radiology film, EKG strip,
# wide spreadsheet) has exactly this geometry and fires it by accident.
#
# DEFAULT_MAX_PAGE_PIXELS — derived from the largest page a clinic plausibly
# scans, at the deployed default DPI (200), then given headroom:
#
#     Letter   8.5 x 11 in  -> 1700 x 2200 =  3.74 MP
#     A4       8.27 x 11.69 -> 1654 x 2338 =  3.87 MP
#     Legal    8.5 x 14     -> 1700 x 2800 =  4.76 MP
#     Tabloid  11 x 17      -> 2200 x 3400 =  7.48 MP   <- the stated worst case
#     ANSI C   17 x 22      -> 3400 x 4400 = 14.96 MP
#     ANSI D   22 x 34      -> 4400 x 6800 = 29.92 MP   <- widest real clinical scan
#                                                          (large-format film / EKG)
#
# 50 MP clears every one of them: 6.7x headroom over tabloid, 3.3x over ANSI C,
# 1.67x over ANSI D. Nothing a clinic actually scans is rejected.
#
# The cap also has to be affordable on a 2 GB box. pdfium renders BGRA at 4
# bytes/px, so the bitmap at the cap is 50e6 x 4 = 200 MB, plus the PIL copy and
# the PNG encode buffer -> order 400-600 MB transient for one page. That fits;
# 6.4 GB does not. Both attack geometries are rejected with room to spare:
# 144 MP is 2.9x the cap, 1600 MP is 32x it.
DEFAULT_MAX_PAGE_PIXELS = 50_000_000

# DEFAULT_MAX_PAGES — page count is separately unbounded (the loop below walks
# every page), and a 2000-page bomb is a 244 KB upload. The cap must NOT reject
# real medicine: a long discharge summary is genuinely 50-100 pages, and a
# 300-page summary is ordinary. 1000 gives 3.3x headroom over that 300-page
# ordinary case and 10x over a typical long one, while rejecting the 2000-page
# bomb. Cost at the cap: this function accumulates every page's PNG in a list
# before returning, and a scanned letter page at 200 DPI encodes to roughly
# 0.1-0.6 MB, so 1000 pages is ~0.1-0.6 GB of accumulated PNG.
DEFAULT_MAX_PAGES = 1000


@dataclass(frozen=True)
class RasterizedPage:
    """One rendered page: its 1-based number, pixel size, and PNG bytes."""

    page_no: int
    width: int
    height: int
    image: bytes


class RasterizationError(RuntimeError):
    """The bytes could not be rendered to page images (not a readable PDF)."""


def rasterize_pdf(
    content: bytes,
    dpi: int = DEFAULT_DPI,
    *,
    max_page_pixels: int = DEFAULT_MAX_PAGE_PIXELS,
    max_pages: int = DEFAULT_MAX_PAGES,
) -> list[RasterizedPage]:
    """Render ``content`` to one :class:`RasterizedPage` per page, portrait-preserving.

    Deterministic: identical ``content`` + ``dpi`` yields byte-identical PNGs.
    Raises :class:`RasterizationError` on empty/invalid input, on a document with
    more than ``max_pages`` pages, or on a page whose rendered pixel area would
    exceed ``max_page_pixels``.

    Both caps are enforced BEFORE the offending allocation happens — the page
    count is checked before any page renders, and each page's area is checked
    from its declared geometry before ``render`` is called. Rejecting after the
    fact would be no defence at all: the render IS the OOM.
    """
    if dpi <= 0:
        raise RasterizationError("render DPI must be positive")
    scale = dpi / _POINTS_PER_INCH
    try:
        pdf = pdfium.PdfDocument(content)
    except Exception as exc:  # pdfium raises PdfiumError on malformed input
        raise RasterizationError("could not open document for rasterization") from exc
    try:
        page_count = len(pdf)
        if page_count == 0:
            raise RasterizationError("document has no pages to rasterize")
        if page_count > max_pages:
            raise RasterizationError(
                f"document has {page_count} pages, exceeding the {max_pages}-page "
                f"rasterization limit"
            )
        pages: list[RasterizedPage] = []
        for index in range(page_count):
            page = pdf[index]
            try:
                # Checked from the DECLARED geometry, before render allocates.
                # pdfium ceils each rendered dimension, so this can under-count
                # the true render by ~(w+h) pixels (A4 at 200 DPI computes 1654
                # wide, renders 1655). That is ~0.03% at the cap's scale and is
                # swamped by the cap's headroom — it does not weaken the bound.
                width_pt, height_pt = page.get_size()
                page_pixels = (width_pt * scale) * (height_pt * scale)
                if page_pixels > max_page_pixels:
                    raise RasterizationError(
                        f"page {index + 1} would render to {page_pixels / 1e6:.1f} "
                        f"megapixels at {dpi} DPI, exceeding the "
                        f"{max_page_pixels / 1e6:.1f} megapixel per-page limit"
                    )
                bitmap = page.render(scale=scale)
                pil_image = bitmap.to_pil()
                width, height = pil_image.size
                buffer = io.BytesIO()
                pil_image.save(buffer, format="PNG")
                pages.append(
                    RasterizedPage(
                        page_no=index + 1,
                        width=int(width),
                        height=int(height),
                        image=buffer.getvalue(),
                    )
                )
            finally:
                page.close()
        return pages
    finally:
        pdf.close()
