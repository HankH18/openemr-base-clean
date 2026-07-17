"""Resilience of the serve path — two robustness properties, pinned.

**Defect 1 (history cap).** Conversation history replayed into the prompt was
UNBOUNDED: ``ChatService`` read every stored message and every agent rendered
them all into the model prompt with no truncation. A long-lived thread grew the
prompt without bound (quadratic token cost) and eventually overflowed the model
context window — a non-retryable 400 that permanently 500s that thread. The cap
lives in ``_to_turns`` (the single point both serve paths assemble history
from), so BOTH the inline and graph paths inherit it.

**Defect 2 (worker isolation).** ``AgentGraph.run`` awaited each worker dispatch
with no try/except, so one worker raising aborted the whole run — a 500 that
also discarded the OTHER worker's completed report. A multi-agent graph exists
to DEGRADE to whatever the surviving workers produced; a transient failure in
one worker must contain to that worker, never sink the turn. The fail-CLOSED
correctness property is untouched: the deterministic verifier still gates every
claim, so a missing worker means only less evidence, never a lowered bar.

DB-free where possible (``AgentGraph.run`` over injected worker doubles, as in
``test_graph``); the end-to-end withhold + the both-paths history cap drive
``ChatService`` against a temp-file SQLite DB, reusing the in-memory FHIR double.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa

from copilot.agent.base import AgentAnswer, ConversationTurn
from copilot.chat.service import _WITHHELD_ANSWER, ChatService, _cap_turns, _to_turns
from copilot.config import Settings, get_settings
from copilot.domain.contracts import VerificationAction
from copilot.domain.documents import ExtractedFact
from copilot.domain.primitives import ClinicianId, FhirReference, PatientId
from copilot.graph.critic import StubCritic
from copilot.graph.evidence_retriever import EvidenceReport
from copilot.graph.intake_extractor import IntakeReport
from copilot.graph.supervisor import AgentGraph, StubSupervisor
from copilot.memory.records import ConversationMessage
from copilot.observability import NoopObservability

# Reuse the deterministic doubles + fixtures the graph/chat tests already pin.
from tests.test_chat_routes import _COHORT, CLIN, SICK, _FakeFhir
from tests.test_graph import (
    _ACS_EVIDENCE,
    _DKA_EVIDENCE,
    _EMPTY_INTAKE,
    GUIDELINE_Q,
    _FakeEvidenceRetriever,
    _FakeIntakeExtractor,
    _keyless,
)

# A question that routes to BOTH workers: guideline intent ("guidelines"/
# "recommend") + a document in scope. "troponin" also matches the cohort's
# Observation, so the CHART alone can ground it — which is what lets a
# both-workers-dead run still serve a fully-grounded chart-only answer.
_BOTH_WORKERS_Q = GUIDELINE_Q  # "What do the guidelines recommend for this troponin result?"
_DOC_IDS = ["5"]  # doubles ignore the ids; their presence is all that routes intake

# A guideline question the CHART cannot ground (no token matches Troponin I /
# aspirin / NSTEMI), so with both workers dead the turn has nothing to serve.
_UNGROUNDABLE_Q = "Is this regimen appropriate per the guidelines?"


class _RaisingIntakeExtractor:
    """Intake-extractor double that always fails mid-dispatch."""

    async def run(self, task: Any) -> IntakeReport:
        raise RuntimeError("intake worker boom")


class _RaisingEvidenceRetriever:
    """Evidence-retriever double that always fails mid-dispatch."""

    async def run(self, task: Any) -> EvidenceReport:
        raise RuntimeError("evidence worker boom")


def _graph(*, intake: Any, evidence: Any) -> AgentGraph:
    """A keyless graph over the in-memory cohort with both workers injected."""
    return AgentGraph(
        settings=_keyless(),
        supervisor=StubSupervisor(),
        intake_extractor=intake,
        evidence_retriever=evidence,
        critic=StubCritic(),
        observability=NoopObservability(),
        fhir_client_factory=lambda: _FakeFhir(_COHORT),
    )


def _potassium_report() -> IntakeReport:
    return IntakeReport(
        document_ids=_DOC_IDS,
        fact_count=1,
        extraction_confidence=0.91,
        facts=[
            ExtractedFact(
                field_path="labs.potassium.value", value="5.6", unit="mmol/L", supported=True
            )
        ],
    )


# ===========================================================================
# Defect 2 — one worker's exception must not abort the whole turn
# ===========================================================================


class TestWorkerIsolation:
    async def test_one_worker_raises_other_survives_and_serves(self) -> None:
        """Intake raises, evidence succeeds → the turn still serves, from the survivor.

        Reverting the try/except in ``AgentGraph.run`` makes ``run`` propagate
        the RuntimeError here — i.e. a 500 — so this red-flags the defect.
        """
        graph = _graph(
            intake=_RaisingIntakeExtractor(),
            evidence=_FakeEvidenceRetriever([_DKA_EVIDENCE]),
        )
        result = await graph.run(
            _task(question=_BOTH_WORKERS_Q, document_ids=_DOC_IDS)
        )

        # Served, and the evidence SURVIVOR's contribution is present in the prose.
        assert result.verification.action == VerificationAction.served
        assert "0.1 units/kg/hour" in result.answer
        assert "Insulin therapy" in result.answer
        assert result.metrics.retrieval_hits == 1
        # The failed intake contributed nothing — contained, not fatal.
        assert result.metrics.extraction_confidence == 0.0
        # Fail-closed intact: every served claim is still FHIR-grounded.
        assert result.verification.claims
        assert all(isinstance(c.source_ref, FhirReference) for c in result.verification.claims)

    async def test_reverse_evidence_raises_intake_survives_and_serves(self) -> None:
        """Symmetric: evidence raises, intake succeeds → the intake survivor lands."""
        graph = _graph(
            intake=_FakeIntakeExtractor(_potassium_report()),
            evidence=_RaisingEvidenceRetriever(),
        )
        result = await graph.run(
            _task(question=_BOTH_WORKERS_Q, document_ids=_DOC_IDS)
        )

        assert result.verification.action == VerificationAction.served
        assert "labs.potassium.value = 5.6 mmol/L" in result.answer
        assert result.metrics.extraction_confidence == 0.91
        assert result.metrics.retrieval_hits == 0  # evidence failed → no hits

    async def test_both_workers_raise_serves_chart_only_when_groundable(self) -> None:
        """Both workers dead + a chart-groundable question → served chart-ONLY.

        The whole point of the graph: it degrades to whatever survives. Here the
        survivor is the chart itself. No worker content leaks in, and the served
        answer is fully FHIR-grounded — never a partial/ungrounded serve.
        """
        graph = _graph(
            intake=_RaisingIntakeExtractor(),
            evidence=_RaisingEvidenceRetriever(),
        )
        result = await graph.run(
            _task(question=_BOTH_WORKERS_Q, document_ids=_DOC_IDS)
        )

        assert result.verification.action == VerificationAction.served
        assert result.verification.claims
        assert all(isinstance(c.source_ref, FhirReference) for c in result.verification.claims)
        # Neither dead worker contributed anything to the prose.
        assert "0.1 units/kg/hour" not in result.answer
        assert "Guideline context" not in result.answer
        assert result.metrics.retrieval_hits == 0
        assert result.metrics.extraction_confidence == 0.0

    async def test_both_workers_raise_fail_safe_when_ungroundable(self) -> None:
        """Both workers dead + an ungroundable question → nothing grounded is served.

        The graph's fail-safe signal is an empty verified-claim set (which the
        chat service maps to a withheld reply — see the end-to-end test below).
        The turn does NOT raise; it degrades, and serves NO ungrounded claim.
        """
        graph = _graph(
            intake=_RaisingIntakeExtractor(),
            evidence=_RaisingEvidenceRetriever(),
        )
        result = await graph.run(
            _task(question=_UNGROUNDABLE_Q, document_ids=_DOC_IDS)
        )

        assert result.verification.claims == []

    async def test_empty_worker_report_is_unchanged(self) -> None:
        """A worker that RETURNS EMPTY (does not raise) behaves exactly as today.

        Distinguishes "worker raised" (contain, degrade) from "worker returned
        empty" (already handled): the empty evidence worker still RAN, so
        ``evidence_retrieved`` is True and the answer is a clean chart-only serve.
        """
        graph = _graph(
            intake=_FakeIntakeExtractor(_EMPTY_INTAKE),
            evidence=_FakeEvidenceRetriever([]),
        )
        result = await graph.run(
            _task(question=_BOTH_WORKERS_Q, document_ids=_DOC_IDS)
        )

        assert result.verification.action == VerificationAction.served
        assert result.evidence_retrieved is True  # ran, returned zero hits
        assert result.guideline_evidence == []
        assert "Guideline context" not in result.answer
        assert result.metrics.retrieval_hits == 0
        assert all(isinstance(c.source_ref, FhirReference) for c in result.verification.claims)

    async def test_degraded_turn_never_serves_a_non_fhir_claim(self) -> None:
        """The verify path is intact: a degraded turn serves ONLY FHIR-grounded claims.

        A dead worker lowers the amount of evidence, never the bar — worker
        output informs prose only, it never becomes a Claim, and the
        deterministic verifier stays the sole authority on what is served.
        """
        graph = _graph(
            intake=_RaisingIntakeExtractor(),
            evidence=_FakeEvidenceRetriever([_DKA_EVIDENCE, _ACS_EVIDENCE]),
        )
        result = await graph.run(
            _task(question=_BOTH_WORKERS_Q, document_ids=_DOC_IDS)
        )

        claim_texts = [c.text for c in result.verification.claims]
        assert all(isinstance(c.source_ref, FhirReference) for c in result.verification.claims)
        # No guideline snippet ever became a claim.
        assert not any("units/kg/hour" in t or "aspirin 162" in t for t in claim_texts)


def _task(*, question: str, document_ids: list[str]) -> Any:
    from copilot.graph.contracts import AgentTask

    return AgentTask(patient_id=SICK, question=question, document_ids=document_ids)


# ===========================================================================
# Defect 1 — history cap: unit coverage of the cap itself
# ===========================================================================


def _msg(role: str, content: str) -> ConversationMessage:
    from copilot.domain.primitives import utcnow

    return ConversationMessage(role=role, content=content, created_at=utcnow())


def _pairs(n: int) -> list[ConversationMessage]:
    """``n`` well-formed user/assistant exchanges (2n messages)."""
    out: list[ConversationMessage] = []
    for i in range(n):
        out.append(_msg("user", f"u{i}"))
        out.append(_msg("assistant", f"a{i}"))
    return out


class TestHistoryCapUnit:
    def test_more_than_cap_keeps_only_the_last_n(self) -> None:
        turns = _to_turns(_pairs(6), 4)  # 12 messages, cap 4
        assert [(t.role, t.content) for t in turns] == [
            ("user", "u4"),
            ("assistant", "a4"),
            ("user", "u5"),
            ("assistant", "a5"),
        ]

    def test_at_cap_is_unchanged(self) -> None:
        msgs = _pairs(2)  # exactly 4 messages
        assert _to_turns(msgs, 4) == _to_turns(msgs, None)
        assert len(_to_turns(msgs, 4)) == 4

    def test_under_cap_is_unchanged(self) -> None:
        msgs = _pairs(1)  # 2 messages, cap 40
        assert len(_to_turns(msgs, 40)) == 2

    def test_none_caps_nothing(self) -> None:
        assert len(_to_turns(_pairs(100), None)) == 200

    def test_leading_dangling_assistant_is_trimmed(self) -> None:
        # An ODD cut into a well-formed thread would start on an assistant turn
        # whose paired user fell off the window; that dangling assistant is
        # dropped so the replay opens on a user turn.
        capped = _cap_turns([ConversationTurn(role=r, content=r) for r in
                             ("user", "assistant", "user", "assistant", "user", "assistant")], 3)
        assert [t.role for t in capped] == ["user", "assistant"]

    def test_non_positive_cap_replays_nothing(self) -> None:
        assert _cap_turns([ConversationTurn(role="user", content="u")], 0) == []
        assert _cap_turns([ConversationTurn(role="user", content="u")], -5) == []


# ===========================================================================
# Defect 1 + 2 — DB-backed: both serve paths honour the cap; both-dead withholds
# ===========================================================================


@pytest.fixture
def _db(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Temp SQLite file + created schema; keyless settings so every factory stubs."""
    from copilot.memory.db import Base, get_engine, get_session_factory

    db_file = tmp_path / "graph_resilience.db"
    monkeypatch.setenv("COPILOT_DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
    monkeypatch.setenv("COPILOT_FHIR_BASE_URL", "http://oe.test/fhir")
    monkeypatch.setenv("COPILOT_ANTHROPIC_API_KEY", "")
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


class _RecordingAgent:
    """Chat-agent double that records the history it was handed each call."""

    def __init__(self, sink: list[list[ConversationTurn]]) -> None:
        self._sink = sink

    async def answer(
        self,
        patient_id: PatientId,
        message: str,
        history: list[ConversationTurn] | None = None,
        *,
        guideline_evidence: Any = None,
        document_facts: Any = None,
    ) -> AgentAnswer:
        self._sink.append(list(history or []))
        return AgentAnswer(answer="ok", claims=[])


def _settings(*, graph_enabled: bool, max_turns: int) -> Settings:
    return get_settings().model_copy(
        update={"chat_graph_enabled": graph_enabled, "chat_history_max_turns": max_turns}
    )


async def _seed_conversation(n_exchanges: int) -> int:
    """Create a SICK-owned conversation seeded with ``n_exchanges`` u/a pairs."""
    from copilot.memory.db import session_scope
    from copilot.memory.repository import MemoryRepository

    async with session_scope() as session:
        repo = MemoryRepository(session)
        conv_id = await repo.create_conversation(
            ClinicianId(value=CLIN), PatientId(value=SICK), "resilience-seed-0001"
        )
        for i in range(n_exchanges):
            await repo.append_message(conv_id, "user", f"u{i}")
            await repo.append_message(conv_id, "assistant", f"a{i}")
    return conv_id


class TestBothPathsHonourHistoryCap:
    async def test_inline_path_caps_replayed_history(
        self, _db: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sink: list[list[ConversationTurn]] = []
        monkeypatch.setattr(
            "copilot.chat.service.build_agent", lambda settings, fhir: _RecordingAgent(sink)
        )
        conv_id = await _seed_conversation(6)  # 12 messages
        service = ChatService(
            _settings(graph_enabled=False, max_turns=4),
            NoopObservability(),
            fhir_client_factory=lambda: _FakeFhir(_COHORT),
        )
        await service.chat(
            clinician_id=ClinicianId(value=CLIN),
            patient_id=PatientId(value=SICK),
            message="What is the latest troponin value?",
            correlation_id="resilience-inline-0001",
            conversation_id=conv_id,
        )

        assert len(sink) == 1
        assert [(t.role, t.content) for t in sink[0]] == [
            ("user", "u4"),
            ("assistant", "a4"),
            ("user", "u5"),
            ("assistant", "a5"),
        ]

    async def test_graph_path_caps_replayed_history(
        self, _db: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sink: list[list[ConversationTurn]] = []
        monkeypatch.setattr(
            "copilot.graph.supervisor.build_agent", lambda settings, fhir: _RecordingAgent(sink)
        )
        conv_id = await _seed_conversation(6)  # 12 messages
        service = ChatService(
            _settings(graph_enabled=True, max_turns=4),
            NoopObservability(),
            fhir_client_factory=lambda: _FakeFhir(_COHORT),
        )
        await service.chat(
            clinician_id=ClinicianId(value=CLIN),
            patient_id=PatientId(value=SICK),
            message="What is the latest troponin value?",  # chart-only → straight to finalize
            correlation_id="resilience-graph-0001",
            conversation_id=conv_id,
        )

        assert len(sink) == 1
        assert [(t.role, t.content) for t in sink[0]] == [
            ("user", "u4"),
            ("assistant", "a4"),
            ("user", "u5"),
            ("assistant", "a5"),
        ]

    async def test_short_history_is_replayed_in_full(
        self, _db: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression guard: a thread at/under the cap is unchanged."""
        sink: list[list[ConversationTurn]] = []
        monkeypatch.setattr(
            "copilot.chat.service.build_agent", lambda settings, fhir: _RecordingAgent(sink)
        )
        conv_id = await _seed_conversation(2)  # 4 messages, cap 4
        service = ChatService(
            _settings(graph_enabled=False, max_turns=4),
            NoopObservability(),
            fhir_client_factory=lambda: _FakeFhir(_COHORT),
        )
        await service.chat(
            clinician_id=ClinicianId(value=CLIN),
            patient_id=PatientId(value=SICK),
            message="What is the latest troponin value?",
            correlation_id="resilience-short-0001",
            conversation_id=conv_id,
        )

        assert [(t.role, t.content) for t in sink[0]] == [
            ("user", "u0"),
            ("assistant", "a0"),
            ("user", "u1"),
            ("assistant", "a1"),
        ]


class TestChatServiceDegradesToWithhold:
    async def test_both_workers_raise_withholds_end_to_end(
        self, _db: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Both workers dead + an ungroundable question → an honest WITHHELD reply.

        End-to-end through ChatService: the turn does NOT 500, and it never serves
        a partial/ungrounded answer as complete.
        """

        def _raising_graph(
            settings: Settings,
            *,
            observability: Any = None,
            fhir_client_factory: Any = None,
        ) -> AgentGraph:
            return AgentGraph(
                settings=settings,
                supervisor=StubSupervisor(),
                intake_extractor=_RaisingIntakeExtractor(),
                evidence_retriever=_RaisingEvidenceRetriever(),
                critic=StubCritic(),
                observability=observability or NoopObservability(),
                fhir_client_factory=fhir_client_factory,
            )

        monkeypatch.setattr("copilot.chat.service.build_graph", _raising_graph)
        service = ChatService(
            _settings(graph_enabled=True, max_turns=40),
            NoopObservability(),
            fhir_client_factory=lambda: _FakeFhir(_COHORT),
        )
        reply = await service.chat(
            clinician_id=ClinicianId(value=CLIN),
            patient_id=PatientId(value=SICK),
            message=_UNGROUNDABLE_Q,
            correlation_id="resilience-withhold-0001",
            document_ids=_DOC_IDS,
        )

        assert reply.action == VerificationAction.withheld
        assert reply.claims == []
        assert reply.answer == _WITHHELD_ANSWER
