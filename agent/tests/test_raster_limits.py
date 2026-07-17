"""Rasterization resource caps + the ingest event-loop offload.

Two live denial-of-service defects, both reachable by BENIGN clinical input:

1. **Unbounded pixel area.** A PDF page's MediaBox costs the uploader nothing but
   the render is quadratic in it. Against the shipped ``rasterize_pdf`` at the
   deployed default ``ocr_dpi=200``, a 544-byte one-page PDF declaring a 60x60in
   MediaBox rendered to 12000x12000 (144 MP) with a measured 1.10 GB peak RSS; a
   546-byte 200x200in page (legal per the PDF spec) asks for 40000x40000 = 1.6
   gigapixel / 6.4 GB. The droplet is 1 vCPU / 2 GB with no ``mem_limit`` on the
   agent container, so a sub-kilobyte upload OOM-kills the process and every
   clinician loses chat, rounds, and document reads until restart. The only
   pre-existing guard was a BYTE cap (Caddy ``max_size 25MB``) and is inert
   against a 544-byte payload. This is not primarily an attack: a large-format
   scan (radiology film, EKG strip, wide spreadsheet) has exactly this geometry.

2. **Blocking the event loop.** ``rasterize_pdf`` and ``OcrEngine.recognize`` are
   synchronous and CPU-bound, and were called straight from the ``_rasterize_and_ocr``
   coroutine that the upload route awaits inline. A 36.5 KB / 300-page PDF — an
   ordinary discharge summary — blocked the loop for 8151 ms on raster ALONE
   (OCR excluded), stalling concurrent requests by up to 8102 ms.

The area/page-count tests here assert the guard fires BEFORE the allocation, by
making ``render`` itself an error. A test that merely catches the raise after a
1.1 GB render would prove nothing — the render IS the OOM.
"""

from __future__ import annotations

import asyncio
import io
import threading
import time
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pypdfium2 as pdfium  # type: ignore[import-untyped]  # ships no py.typed marker
import pytest
import sqlalchemy as sa

from copilot.config import Settings
from copilot.documents.ocr import OcrToken
from copilot.documents.raster import (
    DEFAULT_MAX_PAGE_PIXELS,
    DEFAULT_MAX_PAGES,
    RasterizationError,
    rasterize_pdf,
)

_POINTS_PER_INCH = 72.0

# The deployed default (config.py ``ocr_dpi``). Every cap here is derived at it.
DEPLOYED_DPI = 200


def _pdf_of_size(width_in: float, height_in: float, pages: int = 1) -> bytes:
    """A minimal valid PDF of ``pages`` pages, each ``width_in`` x ``height_in``.

    Built through pdfium so the geometry is real (a real MediaBox that a real
    render would honour), not a hand-forged string the parser might reject.
    """
    pdf = pdfium.PdfDocument.new()
    for _ in range(pages):
        page = pdf.new_page(width_in * _POINTS_PER_INCH, height_in * _POINTS_PER_INCH)
        page.close()
    buffer = io.BytesIO()
    pdf.save(buffer)
    pdf.close()
    return buffer.getvalue()


@pytest.fixture
def _no_render_allowed(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[float]]:
    """Make ``PdfPage.render`` an error, and record any attempt to call it.

    This is the whole point of the area/page-count tests: the guard must reject
    from the DECLARED geometry, before pdfium allocates the bitmap. If the guard
    were checked after the render, these tests would see the recorded call (and
    the suite would have quietly allocated the gigabyte we are defending against).
    """
    attempts: list[float] = []

    def _boom(self: Any, *args: Any, **kwargs: Any) -> Any:
        attempts.append(float(kwargs.get("scale", 0.0)))
        raise AssertionError(
            "page.render() was called — the cap did not reject before allocation"
        )

    monkeypatch.setattr(pdfium.PdfPage, "render", _boom)
    yield attempts


# --- Defect 1: unbounded pixel area -----------------------------------------


def test_oversized_page_area_raises_before_any_render(_no_render_allowed: list[float]) -> None:
    """The 544-byte / 60x60in OOM payload: rejected, and NOTHING is rendered."""
    content = _pdf_of_size(60, 60)
    # The payload really is sub-kilobyte — i.e. the byte cap at the proxy
    # (Caddy `max_size 25MB`) can never see this coming.
    assert len(content) < 1024

    with pytest.raises(RasterizationError) as excinfo:
        rasterize_pdf(content, dpi=DEPLOYED_DPI)

    assert _no_render_allowed == [], "the 144 MP render must never be attempted"
    assert "megapixel" in str(excinfo.value)


def test_pdf_spec_maximum_page_is_rejected_before_render(_no_render_allowed: list[float]) -> None:
    """200x200in is legal per the PDF spec; at 200 DPI it is 1.6 gigapixel / 6.4 GB."""
    content = _pdf_of_size(200, 200)
    assert len(content) < 1024

    with pytest.raises(RasterizationError):
        rasterize_pdf(content, dpi=DEPLOYED_DPI)

    assert _no_render_allowed == [], "the 1.6 gigapixel render must never be attempted"


def test_area_cap_is_enforced_on_a_later_page_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """A normal first page must not buy an oversized second page a free render.

    Page 1 is legitimate and renders for real (letter is cheap); the assertion is
    that exactly ONE render happens — page 2's 144 MP never does.
    """
    rendered: list[int] = []
    real_render = pdfium.PdfPage.render

    def _recording_render(self: Any, *args: Any, **kwargs: Any) -> Any:
        rendered.append(id(self))
        return real_render(self, *args, **kwargs)

    monkeypatch.setattr(pdfium.PdfPage, "render", _recording_render)

    pdf = pdfium.PdfDocument.new()
    for width_in, height_in in ((8.5, 11), (60, 60)):
        page = pdf.new_page(width_in * _POINTS_PER_INCH, height_in * _POINTS_PER_INCH)
        page.close()
    buffer = io.BytesIO()
    pdf.save(buffer)
    pdf.close()

    with pytest.raises(RasterizationError) as excinfo:
        rasterize_pdf(buffer.getvalue(), dpi=DEPLOYED_DPI)

    assert "page 2" in str(excinfo.value)
    assert len(rendered) == 1, "only the legitimate page 1 may render"


# --- Defect 1b: unbounded page count ----------------------------------------


def test_page_count_over_cap_raises_before_any_render(_no_render_allowed: list[float]) -> None:
    """The 2000-page bomb is a ~244 KB upload; reject it before page 1 renders."""
    content = _pdf_of_size(8.5, 11, pages=DEFAULT_MAX_PAGES + 1)

    with pytest.raises(RasterizationError) as excinfo:
        rasterize_pdf(content, dpi=DEPLOYED_DPI)

    assert _no_render_allowed == [], "no page may render once the count cap is blown"
    assert "page" in str(excinfo.value)


# --- The regression guard: real medicine still ingests -----------------------


def test_normal_letter_document_still_rasterizes_unchanged() -> None:
    """A 3-page US Letter document at the deployed DPI — the thing we must NOT break.

    This is the guard that proves the caps did not simply break ingestion.
    """
    pages = rasterize_pdf(_pdf_of_size(8.5, 11, pages=3), dpi=DEPLOYED_DPI)

    assert [page.page_no for page in pages] == [1, 2, 3]
    for page in pages:
        # 8.5in x 200 DPI = 1700, 11in x 200 DPI = 2200.
        assert (page.width, page.height) == (1700, 2200)
        assert page.image.startswith(b"\x89PNG"), "still a real PNG render"


def test_a4_document_still_rasterizes() -> None:
    """A4 (the rest of the world's letter) must ingest too.

    Sized within a pixel: pdfium CEILS the rendered dimensions, so 8.27in at 200
    DPI computes to 1654.0 but renders 1655. That rounding is why the area guard
    can under-count the true render by ~(w+h) pixels — 0.03% at the cap's scale,
    swamped by the 1.67x headroom the cap already carries.
    """
    pages = rasterize_pdf(_pdf_of_size(8.27, 11.69), dpi=DEPLOYED_DPI)
    assert len(pages) == 1
    assert pages[0].width == pytest.approx(1654, abs=1)
    assert pages[0].height == pytest.approx(2338, abs=1)


@pytest.mark.parametrize(
    ("name", "width_in", "height_in", "megapixels"),
    [
        ("letter", 8.5, 11, 3.74),
        ("a4", 8.27, 11.69, 3.87),
        ("legal", 8.5, 14, 4.76),
        ("tabloid", 11, 17, 7.48),
        ("ansi_c", 17, 22, 14.96),
        # The widest thing a clinic realistically scans: large-format film / EKG.
        ("ansi_d", 22, 34, 29.92),
    ],
)
def test_default_area_cap_admits_every_real_clinical_page_size(
    name: str, width_in: float, height_in: float, megapixels: float
) -> None:
    """The cap is derived from real geometry — pin the arithmetic, not a vibe.

    Each of these renders at the deployed 200 DPI and must sit under the 50 MP
    default with headroom. If someone lowers the cap to "harden" it, this fails
    and names the clinical document they just made un-ingestable.
    """
    scale = DEPLOYED_DPI / _POINTS_PER_INCH
    rendered = (width_in * _POINTS_PER_INCH * scale) * (height_in * _POINTS_PER_INCH * scale)
    assert rendered == pytest.approx(megapixels * 1e6, rel=0.01), (
        f"{name}: stated arithmetic does not match"
    )
    assert rendered < DEFAULT_MAX_PAGE_PIXELS, f"{name} is a real document and must ingest"


def test_default_page_cap_admits_a_long_discharge_summary() -> None:
    """A long discharge summary is genuinely 50-100 pages; 300 is still ordinary.

    The cap must reject the 2000-page bomb without rejecting real medicine.
    """
    assert DEFAULT_MAX_PAGES > 300, "a 300-page discharge summary is an ordinary document"
    assert DEFAULT_MAX_PAGES >= 100 * 10, "keep real headroom over a typical long summary"
    assert DEFAULT_MAX_PAGES < 2000, "the 2000-page bomb must still be rejected"


# --- Configurability (an operator on a 4 GB box, no code change) -------------


def test_caps_default_to_the_derived_values_in_settings() -> None:
    """Settings must carry the derived defaults — and not drift from raster.py.

    config.py cannot import raster.py (copilot.documents.__init__ imports ocr,
    which imports config — a cycle), so the numbers are written in both places.
    This test is what keeps them equal.
    """
    settings = Settings(database_url="sqlite+aiosqlite:///:memory:")
    assert settings.raster_max_page_pixels == DEFAULT_MAX_PAGE_PIXELS
    assert settings.raster_max_pages == DEFAULT_MAX_PAGES


def test_operator_can_raise_the_area_cap_without_a_code_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bigger box should be able to accept what the 2 GB default refuses."""
    monkeypatch.setenv("COPILOT_RASTER_MAX_PAGE_PIXELS", "200000000")
    settings = Settings(database_url="sqlite+aiosqlite:///:memory:")
    assert settings.raster_max_page_pixels == 200_000_000

    # 60x60in at 200 DPI = 144 MP: over the 50 MP default, under a raised 200 MP.
    content = _pdf_of_size(60, 60)
    with pytest.raises(RasterizationError):
        rasterize_pdf(content, dpi=DEPLOYED_DPI, max_page_pixels=DEFAULT_MAX_PAGE_PIXELS)
    # Not rendered here on purpose — asserting the cap ADMITS it is enough; actually
    # rendering 144 MP would put the 1.1 GB allocation back into the test suite.
    scale = DEPLOYED_DPI / _POINTS_PER_INCH
    assert (60 * 72 * scale) * (60 * 72 * scale) < settings.raster_max_page_pixels


def test_operator_can_raise_the_page_cap_without_a_code_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COPILOT_RASTER_MAX_PAGES", "5000")
    settings = Settings(database_url="sqlite+aiosqlite:///:memory:")
    assert settings.raster_max_pages == 5000

    # Under the default cap this is a rejection; with the raised cap it renders.
    content = _pdf_of_size(8.5, 11, pages=3)
    with pytest.raises(RasterizationError):
        rasterize_pdf(content, dpi=DEPLOYED_DPI, max_pages=2)
    assert len(rasterize_pdf(content, dpi=DEPLOYED_DPI, max_pages=settings.raster_max_pages)) == 3


# --- The pipeline: fail CLOSED, not a 500 ------------------------------------


def _clear_db_caches() -> None:
    from copilot.config import get_settings
    from copilot.memory.db import get_engine, get_session_factory

    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()


@pytest.fixture
def raster_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Temp-file SQLite DB with the document tables pre-created; caches cleared."""
    db_file = tmp_path / "raster_limits.db"
    monkeypatch.setenv("COPILOT_DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
    _clear_db_caches()

    import copilot.memory.models  # noqa: F401  (registers tables on Base.metadata)
    from copilot.memory.db import Base

    engine = sa.create_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(engine)
    engine.dispose()
    yield db_file
    _clear_db_caches()


class _FakeOcr:
    """Deterministic OCR that never decodes the image; records its thread."""

    def __init__(self, sleep_s: float = 0.0) -> None:
        self._sleep_s = sleep_s
        self.threads: list[int] = []

    def recognize(
        self,
        image: bytes,
        page_no: int = 0,
        width: int | None = None,
        height: int | None = None,
    ) -> list[OcrToken]:
        self.threads.append(threading.get_ident())
        if self._sleep_s:
            time.sleep(self._sleep_s)
        return [OcrToken(text="Hemoglobin", bbox=[0.10, 0.10, 0.20, 0.03], conf=0.98)]


class _FakeVision:
    model_name = "stub-vision-1"

    async def extract(self, pages: Sequence[Any], doc_type: Any) -> Any:
        from copilot.documents.vision import LabReport
        from copilot.domain.documents import ExtractedFact

        return LabReport(
            facts=[ExtractedFact(field_path="hemoglobin", value="13.5", unit="g/dL", page_no=1)]
        )


def _service(ocr: Any, settings: Settings | None = None) -> Any:
    from copilot.documents.pipeline import DerivedOnlyUploader, DocumentIngestionService

    return DocumentIngestionService(
        settings or Settings(database_url="sqlite+aiosqlite:///:memory:"),
        write_client_factory=DerivedOnlyUploader,
        ocr=ocr,
        vision=_FakeVision(),
    )


@pytest.mark.asyncio
async def test_pipeline_fails_closed_on_oversized_page(
    raster_db: Path, _no_render_allowed: list[float]
) -> None:
    """RasterizationError must land as status='failed' — recorded, not a silent 500.

    The route awaits `attach_and_extract` inline, so if the pipeline did NOT
    convert this into a recorded failed attempt, the oversized upload would
    surface as an unhandled 500 with a half-built document row behind it.
    """
    from copilot.domain.primitives import PatientId
    from copilot.memory.db import session_scope

    with pytest.raises(RasterizationError):
        await _service(_FakeOcr()).attach_and_extract(
            patient_id=PatientId(value=1015),
            content=_pdf_of_size(60, 60),
            doc_type="lab_pdf",
            correlation_id="corr-raster-oom",
        )

    assert _no_render_allowed == [], "the pipeline must not allocate before failing"

    async with session_scope() as session:
        rows = (await session.execute(sa.text("SELECT status FROM source_document"))).all()
    assert [row[0] for row in rows] == ["failed"], "the attempt must be recorded FAILED"

    # Fail CLOSED: no extraction rows, no orphan facts behind the failure.
    async with session_scope() as session:
        extractions = (await session.execute(sa.text("SELECT id FROM extraction"))).all()
        facts = (await session.execute(sa.text("SELECT id FROM extracted_fact"))).all()
    assert extractions == [], "a failed raster must leave zero extraction rows"
    assert facts == [], "a failed raster must leave zero orphan facts"


@pytest.mark.asyncio
async def test_pipeline_fails_closed_on_page_count_bomb(
    raster_db: Path, _no_render_allowed: list[float]
) -> None:
    from copilot.domain.primitives import PatientId
    from copilot.memory.db import session_scope

    with pytest.raises(RasterizationError):
        await _service(_FakeOcr()).attach_and_extract(
            patient_id=PatientId(value=1016),
            content=_pdf_of_size(8.5, 11, pages=DEFAULT_MAX_PAGES + 1),
            doc_type="lab_pdf",
            correlation_id="corr-raster-pages",
        )

    assert _no_render_allowed == []
    async with session_scope() as session:
        rows = (await session.execute(sa.text("SELECT status FROM source_document"))).all()
    assert [row[0] for row in rows] == ["failed"]


@pytest.mark.asyncio
async def test_pipeline_still_ingests_a_normal_document(raster_db: Path) -> None:
    """The end-to-end regression guard: ordinary letter pages still reach 'extracted'."""
    from copilot.documents.pipeline import IngestionStatus
    from copilot.domain.primitives import PatientId

    result = await _service(_FakeOcr()).attach_and_extract(
        patient_id=PatientId(value=1017),
        content=_pdf_of_size(8.5, 11, pages=2),
        doc_type="lab_pdf",
        correlation_id="corr-raster-ok",
    )
    assert result.status is IngestionStatus.extracted
    assert result.fact_count == 1


# --- Defect 2: the event-loop offload ---------------------------------------


@pytest.mark.asyncio
async def test_render_and_ocr_run_off_the_event_loop_thread(raster_db: Path) -> None:
    """STRUCTURAL and fully deterministic: the sync work executes on a worker thread.

    Not a mock of `to_thread` — this observes the real thread identity of the real
    OCR call made by the real pipeline, so it cannot pass if the offload is removed.
    """
    from copilot.domain.primitives import PatientId

    ocr = _FakeOcr()
    loop_thread = threading.get_ident()

    await _service(ocr).attach_and_extract(
        patient_id=PatientId(value=1018),
        content=_pdf_of_size(8.5, 11, pages=2),
        doc_type="lab_pdf",
        correlation_id="corr-raster-thread",
    )

    assert ocr.threads, "OCR must actually have run"
    assert all(tid != loop_thread for tid in ocr.threads), (
        "render/OCR ran ON the event-loop thread — every concurrent clinician stalls"
    )


@pytest.mark.asyncio
async def test_ingest_does_not_starve_concurrent_requests(raster_db: Path) -> None:
    """BEHAVIOURAL: a concurrent task keeps getting SERVED while an ingest runs.

    The primary signal is ticks-served (throughput), not max-stall, because
    max-stall silently under-reports the very thing it is supposed to catch: a
    starved ticker records no sample for the interval it was starved *through*,
    so a fully-blocked loop can report a flatteringly small "max stall" from the
    handful of ticks around it. Measured on a real 120-page pdfium raster, the
    inline path served 4 ticks with a reported max stall of 1.1 ms — the blocking
    was total, and the stall metric hid it. Ticks-served cannot be fooled that way.

    The blocking work is a `time.sleep` in the fake OCR, which keeps the test fast
    and deterministic. That is a fair stand-in and not a cheat: the real workloads
    release the GIL exactly as sleep does — pdfium renders through ctypes
    (measured: 4 -> 299 ticks served when offloaded) and pytesseract shells out to
    a subprocess. Both assertions below flip if the offload is removed.
    """
    from copilot.domain.primitives import PatientId

    blocking_s = 0.25
    pages = 2
    total_blocking = blocking_s * pages
    tick_s = 0.01
    stalls: list[float] = []

    async def ticker() -> None:
        while True:
            start = time.perf_counter()
            await asyncio.sleep(tick_s)
            stalls.append(time.perf_counter() - start - tick_s)

    tick = asyncio.create_task(ticker())
    await asyncio.sleep(0.05)  # let the ticker reach steady state before we measure
    served_before = len(stalls)
    try:
        await _service(_FakeOcr(sleep_s=blocking_s)).attach_and_extract(
            patient_id=PatientId(value=1019),
            content=_pdf_of_size(8.5, 11, pages=pages),
            doc_type="lab_pdf",
            correlation_id="corr-raster-loop",
        )
    finally:
        tick.cancel()

    served_during = len(stalls) - served_before
    # A free loop serves ~total_blocking/tick_s ticks (~50 here). A loop blocked
    # through the ingest serves a handful. Quarter of ideal is a wide margin
    # either way — measured 45+ offloaded vs ~4 inline.
    ideal = total_blocking / tick_s
    assert served_during > ideal / 4, (
        f"only {served_during} concurrent ticks were served while an ingest carried "
        f"{total_blocking * 1000:.0f} ms of sync work (a free loop serves ~{ideal:.0f}) "
        f"— the work is running ON the event loop and every clinician is starved"
    )
    # Secondary: no single scheduling gap approaches the blocking duration.
    assert stalls and max(stalls) < total_blocking / 4, (
        f"event loop stalled {max(stalls) * 1000:.0f} ms during an ingest carrying "
        f"{total_blocking * 1000:.0f} ms of sync work — it is not offloaded"
    )
