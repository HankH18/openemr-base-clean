"""OCR word-box extraction behind a small Protocol, stub-first.

Two implementations satisfy :class:`OcrEngine` (mirroring the
``build_agent`` / ``build_observability`` stub-vs-real pattern):

- :class:`StubOcr` — deterministic, replays recorded fixture tokens without ever
  decoding the image. No binary required.
- :class:`TesseractOcr` — real local OCR via ``pytesseract`` (PHI never leaves
  the deployment for bounding boxes).

``build_ocr`` selects the stub whenever the ``tesseract`` binary is absent from
``PATH`` — so the whole pipeline runs green on a host with no OCR engine
installed, and transparently upgrades to real OCR where one exists.
"""

from __future__ import annotations

import io
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from copilot.config import Settings
from copilot.documents.fixtures import STUB_PAGE_TOKENS


@dataclass(frozen=True)
class OcrToken:
    """One recognized word: verbatim text, normalized ``[x, y, w, h]`` bbox, conf."""

    text: str
    bbox: list[float]
    conf: float

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the ``document_page.ocr_tokens`` JSON row shape."""
        return {"text": self.text, "bbox": list(self.bbox), "conf": self.conf}


class OcrEngine(Protocol):
    """Contract the ingestion pipeline depends on for page OCR."""

    def recognize(
        self,
        image: bytes,
        page_no: int = 0,
        width: int | None = None,
        height: int | None = None,
    ) -> list[OcrToken]:
        """Return the word boxes recognized on one page image."""
        ...


def _tokens_from_fixture(fixture: Sequence[Mapping[str, Any]]) -> list[OcrToken]:
    return [
        OcrToken(
            text=str(token["text"]),
            bbox=[float(v) for v in token["bbox"]],
            conf=float(token["conf"]),
        )
        for token in fixture
    ]


class StubOcr:
    """Deterministic OCR that replays recorded fixture tokens per page.

    Never decodes the image bytes — the recorded tokens *are* the output — so it
    is fully offline and reproducible. Uses the built-in fixture unless a caller
    injects its own page fixtures.
    """

    def __init__(self, fixture_tokens: Sequence[Sequence[Mapping[str, Any]]] | None = None) -> None:
        pages = fixture_tokens if fixture_tokens is not None else [STUB_PAGE_TOKENS]
        self._pages: list[list[OcrToken]] = [_tokens_from_fixture(page) for page in pages]

    def recognize(
        self,
        image: bytes,
        page_no: int = 0,
        width: int | None = None,
        height: int | None = None,
    ) -> list[OcrToken]:
        if not self._pages:
            return []
        index = page_no if 0 <= page_no < len(self._pages) else 0
        return list(self._pages[index])


class TesseractOcr:
    """Real local OCR via ``pytesseract`` — normalizes pixel boxes to [0, 1].

    Not exercised on a host without the ``tesseract`` binary (``build_ocr`` falls
    back to :class:`StubOcr` there), but imports + type-checks cleanly and runs
    where the binary is present. Confidence is normalized to [0, 1].
    """

    def __init__(self, language: str = "eng") -> None:
        self._language = language

    def recognize(
        self,
        image: bytes,
        page_no: int = 0,
        width: int | None = None,
        height: int | None = None,
    ) -> list[OcrToken]:
        import pytesseract  # type: ignore[import-untyped]  # ships no py.typed marker
        from PIL import Image

        pil_image = Image.open(io.BytesIO(image))
        page_w = float(width or pil_image.width or 1)
        page_h = float(height or pil_image.height or 1)
        data = pytesseract.image_to_data(
            pil_image, lang=self._language, output_type=pytesseract.Output.DICT
        )
        tokens: list[OcrToken] = []
        for i in range(len(data["text"])):
            text = str(data["text"][i]).strip()
            if not text:
                continue
            raw_conf = float(data["conf"][i])
            if raw_conf < 0:  # tesseract emits -1 for non-text regions
                continue
            confidence = raw_conf / 100.0 if raw_conf > 1.0 else raw_conf
            tokens.append(
                OcrToken(
                    text=text,
                    bbox=[
                        float(data["left"][i]) / page_w,
                        float(data["top"][i]) / page_h,
                        float(data["width"][i]) / page_w,
                        float(data["height"][i]) / page_h,
                    ],
                    conf=confidence,
                )
            )
        return tokens


def build_ocr(settings: Settings) -> OcrEngine:
    """Real Tesseract OCR when its binary is on PATH, else the deterministic stub."""
    if shutil.which("tesseract") is None:
        return StubOcr()
    return TesseractOcr(language=settings.ocr_language)
