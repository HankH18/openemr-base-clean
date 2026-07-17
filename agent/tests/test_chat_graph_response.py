"""Graph-mode chat response: ONE retrieval per turn, and observable routing.

Black-box over HTTP, because both properties under test are properties of the
route:

- **Single retrieval.** The route used to retrieve guideline evidence for the
  response block on every turn, while in graph mode the evidence-retriever worker
  retrieved *again* under the supervisor's decision — two retrievals per turn,
  and a displayed block decoupled from what the supervisor actually decided. Graph
  mode must now retrieve exactly once and display the worker's own evidence.
- **Observable handoffs.** The multi-agent routing must be checkable from the
  response, not just from a design doc — and must carry no patient content.

The inline (flag-OFF) path's own retrieval behaviour is pinned unchanged.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from copilot.chat.service import ChatService
from copilot.domain.primitives import GuidelineCitation
from copilot.rag import GuidelineEvidence
from tests.test_chat_routes import _COHORT, CLIN, SICK, _FakeFhir

# Contains "guidelines"/"recommend" -> the deterministic router dispatches the
# evidence-retriever, which is the only way the graph produces evidence.
GUIDELINE_Q = "What do the guidelines recommend for this troponin result?"
# No guideline intent and no documents -> the supervisor dispatches no worker.
CHART_Q = "What is the latest troponin value?"


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
    """A ``GuidelineRetriever`` double that counts every retrieval it performs."""

    def __init__(self) -> None:
        self.queries: list[str] = []

    async def retrieve(self, query: str, top_k: int = 4) -> list[GuidelineEvidence]:
        self.queries.append(query)
        return [_evidence()]


@pytest.fixture
def _counting(monkeypatch: pytest.MonkeyPatch) -> _CountingRetriever:
    """Route BOTH retrieval sites — the route's own and the graph worker's —
    through one shared counter, so a double retrieval is visible as count 2."""
    shared = _CountingRetriever()
    monkeypatch.setattr("copilot.api.routes.chat.build_retriever", lambda _s: shared)
    monkeypatch.setattr("copilot.graph.evidence_retriever.build_retriever", lambda _s: shared)
    return shared


@pytest.fixture
def _db_file(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    from copilot.config import get_settings
    from copilot.memory.db import Base, get_engine, get_session_factory

    db_file = tmp_path / "chat_graph_response.db"
    monkeypatch.setenv("COPILOT_DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
    monkeypatch.setenv("COPILOT_FHIR_BASE_URL", "http://oe.test/fhir")
    monkeypatch.setenv("COPILOT_ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("COPILOT_VOYAGE_API_KEY", "")
    monkeypatch.setenv("COPILOT_COHERE_API_KEY", "")
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


def _client(*, graph: bool, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    from copilot.api.app import create_app
    from copilot.config import get_settings
    from copilot.memory.db import get_engine, get_session_factory

    monkeypatch.setenv("COPILOT_CHAT_GRAPH_ENABLED", "true" if graph else "false")
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()
    return TestClient(create_app(get_settings(), probe_factories=[]))


def _chat(client: TestClient, message: str) -> Any:
    return client.post(
        "/v1/chat", json={"clinician_id": CLIN, "patient_id": SICK, "message": message}
    )


# --- tests ------------------------------------------------------------------


class TestSingleRetrieval:
    def test_graph_turn_retrieves_exactly_once(
        self, _db_file: str, _counting: _CountingRetriever, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One guideline turn in graph mode = ONE retrieval, not two."""
        client = _client(graph=True, monkeypatch=monkeypatch)
        r = _chat(client, GUIDELINE_Q)
        assert r.status_code == 200, r.text
        assert len(_counting.queries) == 1, (
            "a graph turn must retrieve guideline evidence exactly once — the "
            "worker's retrieval IS the displayed block; the route must not "
            f"retrieve a second, decoupled set. Queries: {_counting.queries}"
        )

    def test_displayed_evidence_is_the_workers_evidence(
        self, _db_file: str, _counting: _CountingRetriever, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """What the clinician sees is what the supervisor decided to retrieve."""
        client = _client(graph=True, monkeypatch=monkeypatch)
        body = _chat(client, GUIDELINE_Q).json()

        block = body["guideline_evidence"]
        assert [e["chunk_id"] for e in block] == ["31"]
        assert block[0]["source_type"] == "guideline"

    def test_chart_only_graph_turn_retrieves_not_at_all(
        self, _db_file: str, _counting: _CountingRetriever, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No guideline need -> no worker dispatched -> nothing retrieved, and an
        honestly empty block rather than evidence the supervisor never asked for."""
        client = _client(graph=True, monkeypatch=monkeypatch)
        body = _chat(client, CHART_Q).json()

        assert _counting.queries == [], (
            "a chart-only question routes to no evidence worker; retrieving anyway "
            "is the decoupling this fix removes"
        )
        assert body["guideline_evidence"] == []

    def test_inline_path_still_retrieves_once_at_the_route(
        self, _db_file: str, _counting: _CountingRetriever, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Flag-OFF behaviour is intact: the route retrieves, exactly once."""
        client = _client(graph=False, monkeypatch=monkeypatch)
        body = _chat(client, GUIDELINE_Q).json()

        assert len(_counting.queries) == 1
        assert [e["chunk_id"] for e in body["guideline_evidence"]] == ["31"]
        assert body["handoffs"] == [], "the inline path runs no agents to hand off between"


class TestHandoffsAreObservable:
    def test_handoffs_surface_in_the_response(
        self, _db_file: str, _counting: _CountingRetriever, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The multi-agent routing is checkable from the response itself."""
        client = _client(graph=True, monkeypatch=monkeypatch)
        body = _chat(client, GUIDELINE_Q).json()

        handoffs = body["handoffs"]
        assert handoffs, "graph mode must expose its agent handoffs"
        assert all({"from_agent", "to_agent", "reason", "signals"} == set(h) for h in handoffs)

        targets = [h["to_agent"] for h in handoffs]
        assert "evidence-retriever" in targets, f"guideline question -> evidence worker: {targets}"
        assert "critic" in targets, f"every finalized turn passes the critic: {targets}"
        assert all(h["from_agent"] == "supervisor" for h in handoffs)

    def test_evidence_handoff_explains_why_it_routed(
        self, _db_file: str, _counting: _CountingRetriever, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The router's matched terms ride along, so a routing decision is
        explainable rather than an opaque assertion."""
        client = _client(graph=True, monkeypatch=monkeypatch)
        body = _chat(client, GUIDELINE_Q).json()

        evidence_handoff = next(h for h in body["handoffs"] if h["to_agent"] == "evidence-retriever")
        assert evidence_handoff["reason"]
        assert set(evidence_handoff["signals"]) >= {"guidelines", "recommend"}, (
            f"matched vocabulary terms must be exposed: {evidence_handoff['signals']}"
        )

    def test_handoffs_never_carry_patient_content(
        self, _db_file: str, _counting: _CountingRetriever, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """PHI safety: the typed payload carries the raw question and document
        ids; neither may cross the response boundary."""
        import json

        client = _client(graph=True, monkeypatch=monkeypatch)
        body = _chat(client, GUIDELINE_Q).json()

        serialized = json.dumps(body["handoffs"])
        assert "troponin" not in serialized.lower(), (
            f"the question's clinical content must not ride in handoffs: {serialized}"
        )
        assert str(SICK) not in serialized, "no patient identifier in the routing block"
        assert "payload" not in serialized, "the raw routing payload must not be dumped"


class TestInlineRetrievalTelemetry:
    def test_inline_turn_logs_its_retrieval_hits(
        self, _db_file: str, _counting: _CountingRetriever, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Req 7 — the default path must log retrieval hits, which are only
        knowable where the default path retrieves: the route."""
        from copilot.api.app import create_app
        from copilot.config import get_settings
        from copilot.memory.db import get_engine, get_session_factory

        events: list[dict[str, Any]] = []

        class _Obs:
            from contextlib import asynccontextmanager

            @asynccontextmanager
            async def span(self, name: str, **attrs: Any) -> Any:
                yield _Span()

            def event(self, name: str, **attrs: Any) -> None:
                events.append({"name": name, **attrs})

            def record_verification(self, **_kw: Any) -> None:
                return None

            def record_poller_staleness(self, **_kw: Any) -> None:
                return None

            async def flush(self) -> None:
                return None

        class _Span:
            def set_attribute(self, key: str, value: Any) -> None:
                return None

            def set_output(self, value: Any) -> None:
                return None

        monkeypatch.setenv("COPILOT_CHAT_GRAPH_ENABLED", "false")
        get_settings.cache_clear()
        get_engine.cache_clear()
        get_session_factory.cache_clear()
        app = create_app(get_settings(), probe_factories=[])
        app.state.observability = _Obs()
        client = TestClient(app)

        r = _chat(client, GUIDELINE_Q)
        assert r.status_code == 200

        retrieval = [e for e in events if e["name"] == "chat.retrieval"]
        assert retrieval, f"the inline path must log its retrieval hits; got {events}"
        assert retrieval[0]["retrieval_hits"] == 1
        assert retrieval[0]["correlation_id"] == r.json()["correlation_id"]
