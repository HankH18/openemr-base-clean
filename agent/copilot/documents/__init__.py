"""Week-2 multimodal document-ingestion pipeline.

Uploads a source document to OpenEMR, rasterizes + OCRs its pages, extracts
schema-validated facts with a vision model, reconciles each value back to the
page's OCR tokens (bbox + confidence, or flagged unsupported), and persists the
derived artifacts append-only in the agent store. See ``pipeline.py`` for the
end-to-end contract; ``ocr.py`` / ``vision.py`` for the stub-first collaborators.
"""

from __future__ import annotations

from copilot.documents.ocr import OcrEngine, OcrToken, StubOcr, TesseractOcr, build_ocr
from copilot.documents.pipeline import (
    DocumentIngestionService,
    IngestionResult,
    IngestionStatus,
    attach_and_extract,
)
from copilot.documents.raster import RasterizationError, RasterizedPage, rasterize_pdf
from copilot.documents.reconcile import Reconciliation, reconcile_value
from copilot.documents.vision import (
    ClaudeVision,
    DocumentType,
    StubVision,
    VisionExtractionError,
    VisionExtractor,
    build_vision,
    parse_doc_type,
)

__all__ = [
    "ClaudeVision",
    "DocumentIngestionService",
    "DocumentType",
    "IngestionResult",
    "IngestionStatus",
    "OcrEngine",
    "OcrToken",
    "RasterizationError",
    "RasterizedPage",
    "Reconciliation",
    "StubOcr",
    "StubVision",
    "TesseractOcr",
    "VisionExtractionError",
    "VisionExtractor",
    "attach_and_extract",
    "build_ocr",
    "build_vision",
    "parse_doc_type",
    "rasterize_pdf",
    "reconcile_value",
]
