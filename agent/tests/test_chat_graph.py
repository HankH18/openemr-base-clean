"""Chat-graph wiring — the ``chat_graph_enabled`` flag routes a chat turn
through the hand-rolled multi-agent graph, and the fail-closed reply invariant
is identical in both modes.

Proves the graph is no longer dead in the serve path: with the flag ON a chat
turn actually *runs the graph* (its ``graph.run``/``supervisor.route`` spans and
``worker.handoff``/``graph.telemetry`` events are emitted through the injected
observability), the supervisor dispatches the right worker (evidence on a
guideline question, intake when documents are in scope), and an ungroundable
question still fails closed to the honest withheld reply. With the flag OFF the
inline path runs unchanged — no graph spans, no handoffs.

Reuses the in-memory FHIR double + cohort from ``test_chat_routes`` and drives
``ChatService`` directly (the graph reader is injected via ``fhir_client_factory``,
which both modes honour), so the whole path runs offline with no API key.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
import sqlalchemy as sa

from copilot.chat.service import _WITHHELD_ANSWER, ChatReply, ChatService
from copilot.config import Settings, get_settings
from copilot.domain.primitives import ClinicianId, PatientId

# Reuse the FHIR double + synthetic cohort from the sibling chat-route tests.
from tests.test_chat_routes import _COHORT, CLIN, SICK, _FakeFhir

# --- recording observability ------------------------------------------------


class _RecSpan:
    """Span double — records nothing; we only assert on span names + events."""

    def set_attribute(self, key: str, value: Any) -> None:
        return None

    def set_output(self, value: Any) -> None:
        return None


class _RecordingObs:
    """Records opened spans, emitted events, and verification decisions.

    Satisfies the ``copilot.observability.Observability`` protocol structurally
    so it can be injected into ``ChatService`` in place of a real backend.
    """

    def __init__(self) -> None:
        self.spans: list[str] = []
        self.events: list[dict[str, Any]] = []
        self.verifications: list[dict[str, Any]] = []

    @asynccontextmanager
    async def span(self, name: str, **attributes: Any) -> AsyncIterator[_RecSpan]:
        self.spans.append(name)
        yield _RecSpan()

    def event(self, name: str, **attributes: Any) -> None:
        self.events.append({"name": name, "attrs": dict(attributes)})

    def record_verification(self, *, passed: bool, action: str, patient_id: int) -> None:
        self.verifications.append({"passed": passed, "action": action, "patient_id": patient_id})

    def record_poller_staleness(self, *, patient_id: int, age_seconds: int) -> None:
        return None

    async def flush(self) -> None:
        return None

    # --- assertion helpers ---------------------------------------------------

    def handoffs_to(self, needle: str) -> list[dict[str, Any]]:
        """Recorded ``worker.handoff`` events whose ``to_agent`` matches needle."""
        return [
            ev
            for ev in self.events
            if "handoff" in ev["name"] and needle in str(ev["attrs"].get("to_agent", "")).lower()
        ]

    def event_names(self) -> set[str]:
        return {ev["name"] for ev in self.events}


# --- fixtures ---------------------------------------------------------------


@pytest.fixture
def _db(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Temp SQLite file + created schema; keyless settings so every factory stubs."""
    from copilot.memory.db import Base, get_engine, get_session_factory

    db_file = tmp_path / "chat_graph.db"
    monkeypatch.setenv("COPILOT_DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
    monkeypatch.setenv("COPILOT_FHIR_BASE_URL", "http://oe.test/fhir")
    monkeypatch.setenv("COPILOT_ANTHROPIC_API_KEY", "")  # -> StubAgent + stub workers
    monkeypatch.setenv("COPILOT_VOYAGE_API_KEY", "")
    monkeypatch.setenv("COPILOT_COHERE_API_KEY", "")
    monkeypatch.setenv("COPILOT_LANGFUSE_HOST", "")
    monkeypatch.setenv("COPILOT_LANGFUSE_PUBLIC_KEY", "")
    monkeypatch.setenv("COPILOT_LANGFUSE_SECRET_KEY", "")
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()

    import copilot.memory.models  # noqa: F401  (registers tables on Base.metadata)

    sync_engine = sa.create_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()
    yield str(db_file)
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()


def _settings(*, graph_enabled: bool) -> Settings:
    """Keyless settings with the graph flag flipped, DB url from the fixture env."""
    return get_settings().model_copy(update={"chat_graph_enabled": graph_enabled})


def _service(spy: _RecordingObs, *, graph_enabled: bool) -> ChatService:
    """A ChatService whose FHIR reader (agent + graph) is the in-memory double."""
    return ChatService(
        _settings(graph_enabled=graph_enabled),
        spy,
        fhir_client_factory=lambda: _FakeFhir(_COHORT),
    )


async def _chat(
    service: ChatService,
    message: str,
    *,
    document_ids: list[str] | None = None,
) -> ChatReply:
    return await service.chat(
        clinician_id=ClinicianId(value=CLIN),
        patient_id=PatientId(value=SICK),
        message=message,
        correlation_id="chat-graph-corr-0001",
        document_ids=document_ids,
    )


def _seed_document(db_file: str, patient_id: int = SICK) -> str:
    """Insert an extracted source document (+1 supported fact); return its id."""
    from sqlalchemy.orm import Session

    from copilot.memory.models import ExtractedFactRow, ExtractionRow, SourceDocumentRow

    engine = sa.create_engine(f"sqlite:///{db_file}")
    try:
        with Session(engine) as session:
            doc = SourceDocumentRow(
                patient_id=patient_id,
                openemr_document_id="7001",
                doc_type="lab_pdf",
                filename="outside_labs.pdf",
                content_hash="chat-graph-fixture-hash",
                page_count=1,
                status="extracted",
                correlation_id="chat-graph-seed-0001",
            )
            session.add(doc)
            session.flush()
            extraction = ExtractionRow(
                source_document_id=doc.id,
                schema_version="v1",
                model="stub",
                confidence_overall=0.91,
                status="ok",
                correlation_id="chat-graph-seed-0001",
            )
            session.add(extraction)
            session.flush()
            session.add(
                ExtractedFactRow(
                    extraction_id=extraction.id,
                    field_path="labs.potassium.value",
                    value="5.6",
                    unit="mmol/L",
                    page_no=1,
                    bbox=[0.12, 0.34, 0.2, 0.04],
                    match_confidence=0.97,
                    supported=True,
                )
            )
            session.commit()
            doc_id = doc.id
    finally:
        engine.dispose()
    return str(doc_id)


# --- tests ------------------------------------------------------------------


class TestChatGraphEnabled:
    async def test_flag_on_runs_the_graph_and_serves(self, _db: str) -> None:
        """A grounded turn with the flag ON runs the graph and serves the answer."""
        spy = _RecordingObs()
        reply = await _chat(_service(spy, graph_enabled=True), "What is the latest troponin value?")

        # Reply is correctly served + grounded.
        assert reply.action.value == "served"
        assert reply.passed is True
        assert reply.claims, "a served answer must carry grounded claims"

        # It actually ran the graph: the supervisor's trace spans and the
        # graph.telemetry event are what the inline path never emits.
        assert "graph.run" in spy.spans
        assert "supervisor.route" in spy.spans
        assert "graph.telemetry" in spy.event_names()
        # Graph mode does NOT open the inline "chat" span.
        assert "chat" not in spy.spans

        # De-dup: the graph records verification exactly once; the chat service
        # emits no second event in graph mode.
        assert spy.verifications == [{"passed": True, "action": "served", "patient_id": SICK}]

    async def test_guideline_question_dispatches_evidence_worker(self, _db: str) -> None:
        """'guideline'/'recommend' routes the supervisor to the evidence worker."""
        spy = _RecordingObs()
        reply = await _chat(
            _service(spy, graph_enabled=True),
            "What do the guidelines recommend for this troponin result?",
        )
        assert reply.action.value == "served"
        assert spy.handoffs_to("evidence"), "guideline question must hand off to evidence-retriever"

    async def test_document_in_scope_dispatches_intake_worker(self, _db: str) -> None:
        """document_ids in scope routes the supervisor to the intake worker."""
        doc_id = _seed_document(_db)
        spy = _RecordingObs()
        await _chat(
            _service(spy, graph_enabled=True),
            "What is the latest troponin value?",
            document_ids=[doc_id],
        )
        assert spy.handoffs_to("intake"), "a document in scope must hand off to intake-extractor"

    async def test_fail_closed_withheld_in_graph_mode(self, _db: str) -> None:
        """An ungroundable question fails closed to the honest withheld reply."""
        spy = _RecordingObs()
        reply = await _chat(
            _service(spy, graph_enabled=True), "What did the patient's MRI brain show?"
        )
        assert reply.action.value == "withheld"
        assert reply.passed is False
        assert reply.claims == []
        assert reply.answer == _WITHHELD_ANSWER


class TestChatGraphDisabled:
    async def test_flag_off_uses_inline_path(self, _db: str) -> None:
        """The default (flag OFF) runs the inline path: no graph spans/handoffs."""
        spy = _RecordingObs()
        reply = await _chat(
            _service(spy, graph_enabled=False), "What is the latest troponin value?"
        )
        assert reply.action.value == "served"
        assert reply.claims

        # Inline path opens the "chat" span and never the graph's.
        assert "chat" in spy.spans
        assert "graph.run" not in spy.spans
        assert "supervisor.route" not in spy.spans
        assert spy.events == [], "the inline path emits no graph handoff/telemetry events"
        assert spy.verifications == [{"passed": True, "action": "served", "patient_id": SICK}]
