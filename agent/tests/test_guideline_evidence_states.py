"""The guideline-evidence contract must distinguish routed-zero-hit from never-routed.

THE DEFECT THIS GUARDS
----------------------
``ChatReply.guideline_evidence == []`` used to conflate two states the contract
swears it separates:

- **never-routed** — the supervisor saw no guideline need, so the
  evidence-retriever was never dispatched. Honest "no guidelines apply".
- **routed-but-zero-hit** — the worker RAN and the corpus returned nothing (a
  lost/degraded corpus, or a query that legitimately matched nothing).

Both left ``guideline_evidence`` empty, so a clinician could not tell "no
guidelines apply to this question" from "we lost the guidelines". The fix keeps
``None``/``[]``/non-empty on the list exactly as they were and adds an explicit
``evidence_retrieved`` boolean (``evidence_report is not None`` at the graph
boundary) that carries "did the evidence-retriever actually run?".

WHAT IS ASSERTED HERE
---------------------
1. A never-routed turn is distinguishable from a routed-zero-hit turn
   (``evidence_retrieved`` is the discriminator; both lists stay empty).
2. A routed turn that FOUND chunks is unchanged (regression guard).
3. The route does NOT double-retrieve on a routed-zero-hit turn (retrieval
   count is exactly one — the worker's — never a second route-level retrieval).
4. The deliberate NON-degrade decision: a routed-zero-hit turn still SERVES its
   FHIR-grounded answer. Serve/withhold gates on FHIR claims alone; guideline
   evidence informs the prose only and never becomes a Claim, so the flag makes
   the state observable without changing what is served. (Considered gating on
   hit count; rejected — it would silently withhold answers that serve today.)

Test 1's boolean assertion is the one that "bites": revert the fix and the two
empty states collapse to an identical ``([], evidence_retrieved=False)`` pair.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from copilot.chat.service import ChatService
from copilot.domain.contracts import VerificationAction
from copilot.domain.primitives import GuidelineCitation
from copilot.graph.contracts import AgentTask
from copilot.rag import GuidelineEvidence

# Reuse the established deterministic graph double + synthetic cohort.
from tests.test_chat_routes import _COHORT, CLIN, SICK, _FakeFhir
from tests.test_graph import _DKA_EVIDENCE, GUIDELINE_Q, _graph

# A question with no guideline intent and no documents -> the deterministic
# supervisor dispatches NO worker (never-routed).
CHART_Q = "What is the latest troponin value?"


# ---------------------------------------------------------------------------
# 1-4 at the graph/service boundary: the boolean discriminator, DB-free.
# ---------------------------------------------------------------------------


class TestGraphResultDistinguishesEmptyEvidenceStates:
    async def test_never_routed_turn_is_evidence_retrieved_false(self) -> None:
        """No guideline need -> worker never ran -> False, and an empty block."""
        result = await _graph(evidence=[]).run(AgentTask(patient_id=SICK, question=CHART_Q))

        assert result.guideline_evidence == []
        assert result.evidence_retrieved is False, (
            "a turn the supervisor never routed to the evidence-retriever must "
            "report evidence_retrieved=False"
        )

    async def test_routed_zero_hit_turn_is_evidence_retrieved_true(self) -> None:
        """Worker RAN, corpus returned nothing -> True, with an empty block."""
        result = await _graph(evidence=[]).run(AgentTask(patient_id=SICK, question=GUIDELINE_Q))

        assert result.guideline_evidence == []
        assert result.evidence_retrieved is True, (
            "a guideline question routes to the evidence-retriever; a zero-hit "
            "retrieval still means the worker RAN, so evidence_retrieved=True"
        )

    async def test_the_two_empty_states_are_distinguishable(self) -> None:
        """The whole point: identical empty lists, different retrieval reality.

        This is the assertion that BITES — revert the fix and both turns collapse
        to (guideline_evidence=[], evidence_retrieved=False), indistinguishable.
        """
        never_routed = await _graph(evidence=[]).run(
            AgentTask(patient_id=SICK, question=CHART_Q)
        )
        routed_zero = await _graph(evidence=[]).run(
            AgentTask(patient_id=SICK, question=GUIDELINE_Q)
        )

        # The list alone cannot tell them apart...
        assert never_routed.guideline_evidence == routed_zero.guideline_evidence == []
        # ...the boolean is the ONLY thing that can.
        assert never_routed.evidence_retrieved != routed_zero.evidence_retrieved
        assert (never_routed.evidence_retrieved, routed_zero.evidence_retrieved) == (False, True)

    async def test_routed_with_hits_is_unchanged(self) -> None:
        """Regression guard: a retrieval that found chunks is untouched."""
        result = await _graph(evidence=[_DKA_EVIDENCE]).run(
            AgentTask(patient_id=SICK, question=GUIDELINE_Q)
        )

        assert result.evidence_retrieved is True
        assert [e.chunk_id for e in result.guideline_evidence] == [_DKA_EVIDENCE.chunk_id]

    async def test_routed_zero_hit_still_serves_its_fhir_answer(self) -> None:
        """The deliberate NON-degrade: zero guideline hits does NOT withhold.

        Serve/withhold gates on FHIR claims alone; a routed-zero-hit turn keeps
        its verifier-passed, FHIR-grounded claims and is served exactly as it is
        today. If a later product decision chooses to degrade on hit count, THIS
        assertion is the tripwire that will flag the behaviour change loudly.
        """
        result = await _graph(evidence=[]).run(AgentTask(patient_id=SICK, question=GUIDELINE_Q))

        assert result.evidence_retrieved is True
        assert result.guideline_evidence == []
        assert result.verification.action == VerificationAction.served, (
            "a zero-hit guideline retrieval must NOT withhold a FHIR-grounded "
            "answer — that would silently break answers that serve today"
        )
        assert result.verification.claims, "the FHIR-grounded claims survive a zero-hit retrieval"


# ---------------------------------------------------------------------------
# 3 at the HTTP boundary: no double-retrieval, and the state is observable.
# ---------------------------------------------------------------------------


def _evidence(chunk_id: str = "31") -> GuidelineEvidence:
    return GuidelineEvidence(
        chunk_id=chunk_id,
        document_id="7",
        section="Acute coronary syndromes",
        content="Troponin elevation with ischemic symptoms warrants urgent evaluation.",
        score=0.87,
        citation=GuidelineCitation(
            source_id="7",
            page_or_section="Acute coronary syndromes",
            field_or_chunk_id=chunk_id,
            quote_or_value="Troponin elevation with ischemic symptoms warrants urgent evaluation.",
        ),
    )


class _CountingRetriever:
    """A ``GuidelineRetriever`` double that counts retrievals and returns a fixed set."""

    def __init__(self, hits: list[GuidelineEvidence]) -> None:
        self.queries: list[str] = []
        self._hits = hits

    async def retrieve(self, query: str, top_k: int = 4) -> list[GuidelineEvidence]:
        self.queries.append(query)
        return list(self._hits)


@pytest.fixture
def _db_file(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    from copilot.config import get_settings
    from copilot.memory.db import Base, get_engine, get_session_factory

    db_file = tmp_path / "guideline_evidence_states.db"
    monkeypatch.setenv("COPILOT_DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
    monkeypatch.setenv("COPILOT_FHIR_BASE_URL", "http://oe.test/fhir")
    monkeypatch.setenv("COPILOT_ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("COPILOT_VOYAGE_API_KEY", "")
    monkeypatch.setenv("COPILOT_COHERE_API_KEY", "")
    monkeypatch.setenv("COPILOT_CHAT_GRAPH_ENABLED", "true")
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


@pytest.fixture(autouse=True)
def _fake_fhir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ChatService, "_fhir_client", lambda self: _FakeFhir(_COHORT))


@pytest.fixture(autouse=True)
def _authorize_clinician(_db_file: str) -> None:
    """Seed the rounding cursor so CLIN may chat about the cohort (UC-6)."""
    import asyncio

    from copilot.domain.primitives import ClinicianId
    from copilot.memory.db import get_engine, get_session_factory, session_scope
    from copilot.memory.repository import MemoryRepository

    async def _seed() -> None:
        async with session_scope() as session:
            await MemoryRepository(session).upsert_rounding_cursor(
                ClinicianId(value=CLIN), [int(pid) for pid in _COHORT], 0, []
            )

    asyncio.run(_seed())
    get_engine.cache_clear()
    get_session_factory.cache_clear()


def _graph_client(retriever: _CountingRetriever, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """A graph-mode client with BOTH retrieval sites wired through one counter.

    If the route were to retrieve a second, decoupled set on top of the worker's
    retrieval, the shared counter would see two queries — that is the double
    retrieval this test forbids.
    """
    from copilot.api.app import create_app
    from copilot.config import get_settings

    monkeypatch.setattr("copilot.api.routes.chat.build_retriever", lambda _s: retriever)
    monkeypatch.setattr("copilot.graph.evidence_retriever.build_retriever", lambda _s: retriever)
    get_settings.cache_clear()
    return TestClient(create_app(get_settings(), probe_factories=[]))


def _chat(client: TestClient, message: str) -> Any:
    return client.post(
        "/v1/chat", json={"clinician_id": CLIN, "patient_id": SICK, "message": message}
    )


class TestRouteObservesTheStatesWithoutDoubleRetrieving:
    def test_routed_zero_hit_retrieves_exactly_once_and_reports_true(
        self, _db_file: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The worker retrieves once and finds nothing; the route must NOT retrieve
        again, and the response must show it was a routed-but-zero-hit turn."""
        counting = _CountingRetriever(hits=[])
        body = _chat(_graph_client(counting, monkeypatch), GUIDELINE_Q).json()

        assert len(counting.queries) == 1, (
            "a routed turn retrieves once (the worker); the route must not add a "
            f"second, decoupled retrieval. Queries: {counting.queries}"
        )
        assert body["guideline_evidence"] == []
        assert body["evidence_retrieved"] is True, (
            "the worker ran and found nothing — the response must say so, not read "
            "as 'no guidelines apply'"
        )

    def test_never_routed_turn_reports_false_and_retrieves_not_at_all(
        self, _db_file: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No guideline need -> no worker, no retrieval, and False in the response —
        distinguishable from the routed-zero-hit turn above."""
        counting = _CountingRetriever(hits=[])
        body = _chat(_graph_client(counting, monkeypatch), CHART_Q).json()

        assert counting.queries == [], "a chart-only question routes to no evidence worker"
        assert body["guideline_evidence"] == []
        assert body["evidence_retrieved"] is False

    def test_routed_with_hits_reports_true_and_displays_the_chunks(
        self, _db_file: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression guard at the HTTP boundary: a found chunk is displayed."""
        counting = _CountingRetriever(hits=[_evidence("31")])
        body = _chat(_graph_client(counting, monkeypatch), GUIDELINE_Q).json()

        assert len(counting.queries) == 1
        assert [e["chunk_id"] for e in body["guideline_evidence"]] == ["31"]
        assert body["evidence_retrieved"] is True
