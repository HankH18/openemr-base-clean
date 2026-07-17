"""feat_ingestion criterion 3 — OCR Protocol, stub-first.

`StubOcr` deterministically replays recorded fixture tokens (well-formed
{text, bbox[4], conf} word boxes, never decoding the image), and `build_ocr`
selects the stub when the tesseract binary is absent — the whole suite runs
with no binary installed. FROZEN GOAL HARNESS.
"""

from __future__ import annotations

import shutil

import pytest

from ._helpers import (
    OCR_TOKENS,
    PNG_1PX,
    attr_from,
    call_flex,
    instantiate_flex,
    normalize_tokens,
    require_attr_any,
    resolve_ocr_module,
)


async def test_03_ocr_stub_first(tmp_path, monkeypatch, settings):
    mods = resolve_ocr_module()
    stub_cls = attr_from(mods, ["StubOcr", "OcrStub", "StubOCR"])
    build_ocr = attr_from(mods, ["build_ocr", "make_ocr", "ocr_factory"])
    if stub_cls is None or build_ocr is None:
        pytest.fail(
            "OCR Protocol surface missing: need a StubOcr class and a build_ocr factory "
            "in copilot.documents.* — F3 not implemented"
        )

    # StubOcr replays recorded fixture tokens (built-in fixture, or injected).
    try:
        stub = stub_cls()
    except TypeError:
        stub = instantiate_flex(stub_cls, [(("token", "fixture", "page", "record"), [OCR_TOKENS])])

    method = require_attr_any(
        [stub],
        ["recognize", "ocr", "ocr_page", "extract_tokens", "tokens_for", "run", "__call__"],
        what="StubOcr recognize method",
    )
    semantic = [
        (("image", "png", "data", "content", "page"), PNG_1PX),
        (("page_no", "page_index", "index"), 0),
    ]
    out1 = await call_flex(method, semantic, tmp_path=tmp_path, what="StubOcr.recognize")
    tokens1 = normalize_tokens(out1)
    assert tokens1, "StubOcr must replay non-empty fixture tokens"
    for text, bbox, conf in tokens1:
        assert text, "each OCR token carries text"
        assert len(bbox) == 4, "each OCR token carries a [x, y, w, h] bbox"
        assert 0.0 <= conf <= 1.0, "each OCR token carries a confidence in [0, 1]"

    out2 = await call_flex(method, semantic, tmp_path=tmp_path, what="StubOcr.recognize")
    assert normalize_tokens(out2) == tokens1, "stub replay must be deterministic"

    # build_ocr selects the stub when tesseract is absent from PATH.
    bindir = tmp_path / "emptybin"
    bindir.mkdir()
    monkeypatch.setenv("PATH", str(bindir))
    assert shutil.which("tesseract") is None

    built = await call_flex(
        build_ocr, [(("settings", "config"), settings)], tmp_path=tmp_path, what="build_ocr"
    )
    assert isinstance(built, stub_cls) or "stub" in type(built).__name__.lower(), (
        "build_ocr must fall back to the stub when the tesseract binary is absent "
        f"(got {type(built).__name__})"
    )
