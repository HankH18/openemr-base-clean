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


@dataclass(frozen=True)
class RasterizedPage:
    """One rendered page: its 1-based number, pixel size, and PNG bytes."""

    page_no: int
    width: int
    height: int
    image: bytes


class RasterizationError(RuntimeError):
    """The bytes could not be rendered to page images (not a readable PDF)."""


def rasterize_pdf(content: bytes, dpi: int = DEFAULT_DPI) -> list[RasterizedPage]:
    """Render ``content`` to one :class:`RasterizedPage` per page, portrait-preserving.

    Deterministic: identical ``content`` + ``dpi`` yields byte-identical PNGs.
    Raises :class:`RasterizationError` on empty/invalid input.
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
        pages: list[RasterizedPage] = []
        for index in range(page_count):
            page = pdf[index]
            try:
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
