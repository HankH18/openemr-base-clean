"""The status page, the SLO spans, and the corpus probe must not over-claim.

Three observability findings, each of which had the docs claiming more than the
code delivered:

1. ``/v1/status`` read the Week-1 grounding tier (``eval_results.json``,
   categories invariant/boundary/authorization) instead of the 53-case golden
   set scored on the five mandated rubrics. The retrieval hit rate was
   structurally pinned at 0.0 by a category match that could never fire, and the
   latency numbers came from a committed artifact with nothing saying so.
2. The OBSERVABILITY.md §7.1 SLOs cited ``doc.ingest`` / ``guideline.retrieve``
   spans that no code emitted.
3. ``/ready`` reported ready with an empty guideline corpus — serving zero
   evidence, warning nobody.

These tests pin the fixes: real rubrics on the status page, spans that actually
emit under a real backend and stay free under Noop, and a corpus probe that
grades an empty corpus degraded-but-serving.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

from copilot.api import readiness
from copilot.api.routes.status import _METRIC_SOURCES, _load_eval_aggregates
from copilot.config import Settings
from copilot.observability import NoopObservability
from copilot.observability.base import correlation_id_var
from copilot.observability.langfuse_backend import LangfuseObservability
from copilot.rag.embeddings import StubEmbedder
from copilot.rag.retriever import GuidelineRetriever, build_retriever

# The five rubrics the project mandates — the golden set is scored on exactly
# these, and the status page must surface them rather than the Week-1 tier's
# invariant/boundary/authorization taxonomy.
MANDATED_RUBRICS = {
    "schema_valid",
    "citation_present",
    "factually_consistent",
    "safe_refusal",
    "no_phi_in_logs",
}


# --- Finding 1: /v1/status reports the real rubrics --------------------------


def test_status_eval_by_category_reports_the_five_mandated_rubrics() -> None:
    """The regression: categories were the Week-1 tier's, not the rubric set."""
    by_category, _dataset = _load_eval_aggregates()
    assert set(by_category) == MANDATED_RUBRICS, (
        "the status page must report the five mandated rubrics from the golden "
        f"gate baseline; got {sorted(by_category)}"
    )
    # And explicitly NOT the Week-1 grounding tier's categories.
    assert not {"invariant", "boundary", "authorization"} & set(by_category)


def test_status_eval_is_sourced_from_the_53_case_golden_set() -> None:
    by_category, dataset = _load_eval_aggregates()
    assert dataset["case_count"] == 53
    assert "golden_dataset.jsonl" in dataset["name"]
    for rubric, bucket in by_category.items():
        assert bucket["total"] == 53.0, f"{rubric} must be scored over all 53 cases"
        # pass_rate is a fraction (0..1), consistent with the payload's sibling
        # rates — the artifact stores percentages.
        assert 0.0 <= bucket["pass_rate"] <= 1.0
        assert bucket["pass_rate"] == bucket["passed"] / bucket["total"]


def test_status_labels_every_metric_as_measured_or_recorded() -> None:
    """Honesty contract: no metric is published without saying what it is."""
    for metric, source in _METRIC_SOURCES.items():
        assert source.startswith(("measured:", "recorded:", "unavailable:")), (
            f"{metric} must declare its provenance as measured/recorded/unavailable; got {source!r}"
        )


def test_status_labels_latency_as_a_recorded_artifact_not_telemetry() -> None:
    """`latency_ms` is a committed baseline; the page must not imply otherwise."""
    source = _METRIC_SOURCES["latency_ms"]
    assert source.startswith("recorded:")
    assert "latency_report.json" in source
    assert "NOT live production" in source


def test_status_reports_retrieval_hit_rate_as_unavailable_not_a_measured_zero() -> None:
    """No retrieval telemetry exists, so 0.0 must not read as a measurement."""
    source = _METRIC_SOURCES["retrieval_hit_rate"]
    assert source.startswith("unavailable:"), (
        "retrieval_hit_rate has no honest source and must be labelled unavailable "
        "rather than published as a measured 0.0"
    )
    assert "placeholder" in source


@pytest.mark.asyncio
async def test_status_payload_reports_real_rubrics_and_flags_retrieval(
    rag_db: Path,
) -> None:
    """The served payload — not just the loader — carries the honest shape."""
    from copilot.api.routes.status import _status_payload

    payload = await _status_payload()

    assert set(payload["eval_by_category"]) == MANDATED_RUBRICS
    assert payload["eval_dataset"]["case_count"] == 53
    assert payload["retrieval_hit_rate_available"] is False
    assert payload["metric_sources"]["retrieval_hit_rate"].startswith("unavailable:")
    # The pinned payload contract still holds (the frozen acceptance criterion
    # types these keys), so the honesty additions cannot regress the dashboard.
    assert isinstance(payload["retrieval_hit_rate"], float)
    assert isinstance(payload["ingestion_count"], int)
    assert isinstance(payload["latency_ms"]["p50"], float)
    assert isinstance(payload["latency_ms"]["p95"], float)


# --- Finding 2: the SLO spans actually emit ---------------------------------


class _RecordingSpan:
    def __init__(self, name: str, attributes: dict[str, Any]) -> None:
        self.name = name
        self.attributes = dict(attributes)
        self.output: Any = None

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value

    def set_output(self, value: Any) -> None:
        self.output = value


class _RecordingObservability:
    """Real span semantics (nesting via enclosing context), recorded in memory."""

    def __init__(self) -> None:
        self.spans: list[_RecordingSpan] = []
        self._stack: list[_RecordingSpan] = []
        self.parents: dict[str, str | None] = {}

    @asynccontextmanager
    async def span(self, name: str, **attributes: Any) -> AsyncIterator[_RecordingSpan]:
        rec = _RecordingSpan(name, attributes)
        # Parent == whatever span is still open, mirroring the real backend's
        # enclosing-context nesting.
        self.parents[name] = self._stack[-1].name if self._stack else None
        self.spans.append(rec)
        self._stack.append(rec)
        try:
            yield rec
        finally:
            self._stack.pop()

    def event(self, name: str, **attributes: Any) -> None:
        return

    def record_verification(self, *, passed: bool, action: str, patient_id: int) -> None:
        return

    def record_poller_staleness(self, *, patient_id: int, age_seconds: int) -> None:
        return

    async def flush(self) -> None:
        return

    def named(self, name: str) -> _RecordingSpan:
        return next(s for s in self.spans if s.name == name)


def _clear_db_caches() -> None:
    from copilot.config import get_settings
    from copilot.memory.db import get_engine, get_session_factory

    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()


@pytest.fixture
def rag_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Temp-file SQLite DB with the guideline tables pre-created; caches cleared."""
    db_file = tmp_path / "obs_honesty.db"
    monkeypatch.setenv("COPILOT_DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
    _clear_db_caches()

    import copilot.memory.models  # noqa: F401  (registers tables on Base.metadata)
    from copilot.memory.db import Base

    engine = sa.create_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(engine)
    engine.dispose()
    yield db_file
    _clear_db_caches()


async def _seed_chunks(chunks: list[tuple[str, str]]) -> None:
    from copilot.memory.db import session_scope
    from copilot.memory.repository import MemoryRepository

    stub = StubEmbedder()
    async with session_scope() as session:
        repo = MemoryRepository(session)
        doc = await repo.create_guideline_document(
            title="Test guideline", source="test:obs", license="CC-BY-4.0"
        )
        for index, (section, content) in enumerate(chunks):
            await repo.create_guideline_chunk(
                guideline_document_id=doc.id,
                content=content,
                section=section,
                chunk_index=index,
                embedding=stub.embed([content])[0],
            )


class _IdentityReranker:
    def rerank(self, query: str, documents: Sequence[str]) -> list[str]:
        return [str(d) for d in documents]


def _retriever(obs: Any) -> GuidelineRetriever:
    return GuidelineRetriever(
        settings=Settings(database_url="sqlite+aiosqlite:///:memory:"),
        embedder=StubEmbedder(),
        reranker=_IdentityReranker(),
        observability=obs,
    )


@pytest.mark.asyncio
async def test_guideline_retrieve_span_is_emitted(rag_db: Path) -> None:
    """The SLO cites `guideline.retrieve` — it must actually be emitted."""
    await _seed_chunks(
        [
            ("insulin-therapy", "Continuous intravenous insulin infusion for diabetic ketoacidosis."),
            ("nephrotoxin-stewardship", "Hold nephrotoxins in acute kidney injury; avoid NSAIDs."),
        ]
    )
    obs = _RecordingObservability()
    evidence = await _retriever(obs).retrieve("DKA insulin", top_k=2)

    assert [s.name for s in obs.spans] == ["guideline.retrieve"]
    span = obs.named("guideline.retrieve")
    assert span.attributes["hits"] == len(evidence)
    assert span.attributes["corpus_chunks"] == 2
    assert span.attributes["top_k"] == 2
    assert span.output == {"hits": len(evidence), "corpus_chunks": 2}


@pytest.mark.asyncio
async def test_guideline_retrieve_span_records_an_empty_corpus(rag_db: Path) -> None:
    """A zero-evidence answer must be explained by the trace, not silent."""
    obs = _RecordingObservability()
    evidence = await _retriever(obs).retrieve("DKA insulin")

    assert evidence == []
    span = obs.named("guideline.retrieve")
    assert span.attributes["corpus_chunks"] == 0
    assert span.attributes["hits"] == 0


@pytest.mark.asyncio
async def test_guideline_retrieve_span_carries_no_phi(rag_db: Path) -> None:
    """Span attributes are counts only — never the query or chunk text."""
    await _seed_chunks([("lactate", "Remeasure lactate in sepsis; antibiotics within one hour.")])
    obs = _RecordingObservability()
    query = "Does Jane Q. Patient (MRN 12345678) need lactate remeasured?"
    await _retriever(obs).retrieve(query)

    span = obs.named("guideline.retrieve")
    blob = repr(span.attributes) + repr(span.output)
    for leak in ("Jane", "Patient", "12345678", "lactate", "sepsis"):
        assert leak not in blob, f"{leak!r} leaked into the guideline.retrieve span"


@pytest.mark.asyncio
async def test_guideline_retrieve_is_a_noop_under_noop_observability(rag_db: Path) -> None:
    """A missing observability backend must not change behaviour."""
    await _seed_chunks(
        [
            ("insulin-therapy", "Continuous intravenous insulin infusion for diabetic ketoacidosis."),
            ("nephrotoxin-stewardship", "Hold nephrotoxins in acute kidney injury; avoid NSAIDs."),
        ]
    )
    recorded = await _retriever(_RecordingObservability()).retrieve("DKA insulin", top_k=2)
    noop = await _retriever(NoopObservability()).retrieve("DKA insulin", top_k=2)

    assert [e.chunk_id for e in noop] == [e.chunk_id for e in recorded]
    assert [e.content for e in noop] == [e.content for e in recorded]


def test_build_retriever_defaults_observability_from_settings() -> None:
    """Keyless deploys stay on Noop — the span must cost nothing without creds."""
    keyless = build_retriever(Settings(database_url="sqlite+aiosqlite:///:memory:"))
    assert isinstance(keyless._obs, NoopObservability)


def test_build_retriever_uses_the_real_backend_when_langfuse_is_configured() -> None:
    """The SLO is only real if the shipped call path emits to a real backend.

    `routes/chat.py` and the graph's evidence worker call `build_retriever(settings)`
    with no observability argument, so defaulting it from settings here is what
    makes `guideline.retrieve` reach Langfuse in production.
    """
    keyed = build_retriever(
        Settings(
            database_url="sqlite+aiosqlite:///:memory:",
            langfuse_host="https://cloud.langfuse.com",
            langfuse_public_key="pk-test",
            langfuse_secret_key="sk-test",
        )
    )
    assert isinstance(keyed._obs, LangfuseObservability)


@pytest.mark.asyncio
async def test_guideline_retrieve_span_nests_under_the_correlation_trace(rag_db: Path) -> None:
    """The span must join the existing correlation-id trace, not orphan itself."""
    await _seed_chunks([("lactate", "Remeasure lactate in sepsis.")])

    traces: list[dict[str, Any]] = []
    children: list[dict[str, Any]] = []

    class FakeObs:
        def update(self, **kwargs: Any) -> None:
            return

        def end(self) -> None:
            return

        def span(self, **kwargs: Any) -> FakeObs:
            children.append(kwargs)
            return FakeObs()

    class FakeClient:
        def trace(self, **kwargs: Any) -> FakeObs:
            traces.append(kwargs)
            return FakeObs()

        def event(self, **kwargs: Any) -> None:
            return

        def flush(self) -> None:
            return

    obs = LangfuseObservability(
        host="https://x", public_key="pk", secret_key="sk", client=FakeClient()
    )
    token = correlation_id_var.set("corr-obs-1")
    try:
        # An enclosing span, as the chat/graph path opens before retrieval.
        async with obs.span("chat.answer"):
            await _retriever(obs).retrieve("lactate")
    finally:
        correlation_id_var.reset(token)

    assert traces and traces[0]["id"] == "corr-obs-1", "trace root keyed by correlation id"
    assert [c["name"] for c in children] == ["guideline.retrieve"], (
        "guideline.retrieve must be created as a CHILD of the enclosing span"
    )


# --- Finding 2 (cont.): the ingestion spans ---------------------------------


def _fixture_pdf(text: str = "Hemoglobin 13.5 g/dL") -> bytes:
    """A minimal, deterministic, valid single-page PDF."""
    objs: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [4 0 R] /Count 1 >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 3 0 R >> >> /Contents 5 0 R >>",
    ]
    stream = f"BT /F1 18 Tf 72 720 Td ({text}) Tj ET".encode()
    objs.append(
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream"
    )
    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_pos = len(out)
    out += f"xref\n0 {len(objs) + 1}\n".encode() + b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n"
    ).encode()
    return bytes(out)


def _ingestion_service(obs: Any) -> Any:
    """The pipeline over deterministic, offline collaborators."""
    from copilot.documents.ocr import OcrToken
    from copilot.documents.pipeline import DerivedOnlyUploader, DocumentIngestionService
    from copilot.documents.vision import ExtractionResult, LabReport
    from copilot.domain.documents import ExtractedFact

    class _Ocr:
        def recognize(
            self,
            image: bytes,
            page_no: int = 0,
            width: int | None = None,
            height: int | None = None,
        ) -> list[OcrToken]:
            return [
                OcrToken(text="Hemoglobin", bbox=[0.10, 0.10, 0.20, 0.03], conf=0.98),
                OcrToken(text="13.5", bbox=[0.32, 0.10, 0.06, 0.03], conf=0.97),
            ]

    class _Vision:
        model_name = "stub-vision-1"

        async def extract(self, pages: Sequence[Any], doc_type: Any) -> ExtractionResult:
            return LabReport(
                facts=[
                    ExtractedFact(
                        field_path="hemoglobin",
                        value="13.5",
                        unit="g/dL",
                        page_no=1,
                    )
                ]
            )

    return DocumentIngestionService(
        Settings(database_url="sqlite+aiosqlite:///:memory:"),
        write_client_factory=DerivedOnlyUploader,
        ocr=_Ocr(),
        vision=_Vision(),
        observability=obs,
    )


@pytest.mark.asyncio
async def test_doc_ingest_and_extraction_spans_are_emitted(rag_db: Path) -> None:
    """The SLO cites `doc.ingest` + `extraction.run` — both must emit."""
    from copilot.domain.primitives import PatientId

    obs = _RecordingObservability()
    result = await _ingestion_service(obs).attach_and_extract(
        patient_id=PatientId(value=1015),
        content=_fixture_pdf(),
        doc_type="lab_pdf",
        correlation_id="corr-ingest-1",
    )

    names = [s.name for s in obs.spans]
    assert "doc.ingest" in names, "the ingestion-latency SLO's span must exist"
    assert "extraction.run" in names, "the extraction half of the SLO must exist"
    # extraction.run is opened INSIDE doc.ingest -> it is its child.
    assert obs.parents["extraction.run"] == "doc.ingest"
    assert obs.parents["doc.ingest"] is None

    ingest = obs.named("doc.ingest")
    assert ingest.attributes["doc_type"] == "lab_pdf"
    assert ingest.attributes["patient_id"] == 1015
    assert ingest.attributes["correlation_id"] == "corr-ingest-1"
    assert ingest.attributes["page_count"] == 1
    assert ingest.attributes["fact_count"] == result.fact_count
    assert ingest.attributes["status"] == "extracted"


@pytest.mark.asyncio
async def test_doc_ingest_span_carries_no_phi(rag_db: Path) -> None:
    """Counts, ids and the doc type only — never page text or values."""
    from copilot.domain.primitives import PatientId

    obs = _RecordingObservability()
    await _ingestion_service(obs).attach_and_extract(
        patient_id=PatientId(value=1015),
        content=_fixture_pdf(),
        doc_type="lab_pdf",
        # A filename can itself carry a patient name — it must not reach a span.
        filename="Jane_Q_Patient_MRN12345678_labs.pdf",
        correlation_id="corr-ingest-2",
    )

    blob = "".join(repr(s.attributes) + repr(s.output) for s in obs.spans)
    for leak in ("Jane", "Patient", "MRN12345678", "Hemoglobin", "13.5"):
        assert leak not in blob, f"{leak!r} leaked into an ingestion span"


@pytest.mark.asyncio
async def test_ingestion_is_unchanged_under_noop_observability(rag_db: Path) -> None:
    """A missing/Noop backend must not change ingestion behaviour."""
    from copilot.domain.primitives import PatientId

    noop = await _ingestion_service(NoopObservability()).attach_and_extract(
        patient_id=PatientId(value=1015),
        content=_fixture_pdf(),
        doc_type="lab_pdf",
        correlation_id="corr-noop-1",
    )
    assert noop.status.value == "extracted"
    assert noop.fact_count == 1


def test_ingestion_service_defaults_observability_from_settings() -> None:
    """Keyless stays Noop; a keyed deploy gets the real backend with no call-site edit.

    `routes/documents.py` constructs the service without an observability
    argument, so this default is what makes `doc.ingest` reach Langfuse.
    """
    from copilot.documents.pipeline import DocumentIngestionService

    keyless = DocumentIngestionService(Settings(database_url="sqlite+aiosqlite:///:memory:"))
    assert isinstance(keyless._obs, NoopObservability)

    keyed = DocumentIngestionService(
        Settings(
            database_url="sqlite+aiosqlite:///:memory:",
            langfuse_host="https://cloud.langfuse.com",
            langfuse_public_key="pk-test",
            langfuse_secret_key="sk-test",
        )
    )
    assert isinstance(keyed._obs, LangfuseObservability)


# --- Finding 3: /ready grades an empty corpus -------------------------------


@pytest.mark.asyncio
async def test_probe_guideline_corpus_degraded_when_empty(rag_db: Path) -> None:
    """The cheapest high-value fix: an un-ingested deploy must not read ready."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{rag_db}")
    try:
        dep = await readiness.probe_guideline_corpus(engine)
    finally:
        await engine.dispose()

    assert dep.name == "guideline_corpus"
    assert dep.ok is False
    # Degraded, not down: still serving (honest no-evidence beats a fake cite).
    assert dep.advisory is True
    assert dep.status == "degraded"
    assert "ingest_guidelines.py" in dep.detail, "the detail must name the fix"


@pytest.mark.asyncio
async def test_probe_guideline_corpus_ok_when_populated(rag_db: Path) -> None:
    await _seed_chunks([("lactate", "Remeasure lactate in sepsis."), ("dka", "Insulin infusion.")])
    engine = create_async_engine(f"sqlite+aiosqlite:///{rag_db}")
    try:
        dep = await readiness.probe_guideline_corpus(engine)
    finally:
        await engine.dispose()

    assert dep.ok is True
    assert dep.status == "ok"
    assert "2 chunks" in dep.detail


@pytest.mark.asyncio
async def test_probe_guideline_corpus_is_advisory_when_the_table_is_missing() -> None:
    """A missing table (migrations unapplied) degrades — it never 503s."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        dep = await readiness.probe_guideline_corpus(engine)
    finally:
        await engine.dispose()

    assert dep.ok is False
    assert dep.advisory is True
    assert dep.status == "degraded"


@pytest.mark.asyncio
async def test_empty_corpus_keeps_ready_serving_not_down(rag_db: Path) -> None:
    """End-to-end: an empty corpus is degraded-but-serving on /ready (200)."""
    from fastapi.testclient import TestClient

    from copilot.api.app import create_app

    engine = create_async_engine(f"sqlite+aiosqlite:///{rag_db}")
    try:

        async def _corpus() -> Any:
            return await readiness.probe_guideline_corpus(engine)

        app = create_app(probe_factories=[lambda s: _corpus])
        with TestClient(app) as client:
            resp = client.get("/ready")
        assert resp.status_code == 200, (
            "an empty guideline corpus must NOT pull the service out of rotation; "
            f"got {resp.status_code}"
        )
        entry = next(
            d for d in resp.json()["dependencies"] if d["name"] == "guideline_corpus"
        )
        assert entry["ok"] is False
        assert entry["status"] == "degraded"
        assert "ingest_guidelines.py" in entry["detail"]
    finally:
        await engine.dispose()


def test_guideline_corpus_is_wired_into_the_default_graded_readiness() -> None:
    """A probe nobody runs fixes nothing — it must be in the real /ready."""
    from copilot.api.app import _default_probe_factories

    settings = Settings(database_url="sqlite+aiosqlite:///:memory:")
    names = [f(settings).func.__name__ for f in _default_probe_factories()]
    assert "probe_guideline_corpus" in names
