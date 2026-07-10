"""Chat feature tests — grounded drill-down + fail-closed serve-time verification.

Drives the real FastAPI app + repository against a temp-file SQLite DB, with the
FHIR reader replaced by an in-memory double (monkeypatching
``ChatService._fhir_client``). No Anthropic key is set, so ``build_agent`` returns
the deterministic ``StubAgent`` and the whole chat path runs offline.

The double serves two roles the chat path exercises: ``search`` (the agent's
per-type pull) and ``read`` (the verifier's serve-time re-fetch by ID). Giving
``read`` a *drifted* copy lets us prove the fail-closed re-verification: an
answer whose cited value no longer matches the live record is withheld.
"""

from __future__ import annotations

import copy
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from copilot.chat.service import ChatService
from copilot.domain.primitives import ResourceType

CLIN = 8001
SICK = 1001


# --- synthetic FHIR helpers ------------------------------------------------


def _obs(rid: str, text: str, value: float, unit: str, interp: str | None) -> dict[str, Any]:
    res: dict[str, Any] = {
        "resourceType": "Observation",
        "id": rid,
        "meta": {"lastUpdated": "2026-07-09T06:30:00Z"},
        "status": "final",
        "code": {"text": text},
        "valueQuantity": {"value": value, "unit": unit},
    }
    if interp is not None:
        res["interpretation"] = [{"coding": [{"code": interp}]}]
    return res


def _med(rid: str, name: str) -> dict[str, Any]:
    return {
        "resourceType": "MedicationRequest",
        "id": rid,
        "meta": {"lastUpdated": "2026-07-08T20:00:00Z"},
        "status": "active",
        "medicationCodeableConcept": {"text": name},
    }


def _cond(rid: str, name: str) -> dict[str, Any]:
    return {
        "resourceType": "Condition",
        "id": rid,
        "meta": {"lastUpdated": "2026-07-08T20:00:00Z"},
        "clinicalStatus": {"coding": [{"code": "active"}]},
        "code": {"text": name},
    }


# 1001: NSTEMI, critical troponin present + aspirin — enough to ground drill-downs.
_COHORT: dict[str, dict[str, list[dict[str, Any]]]] = {
    "1001": {
        "Observation": [_obs("obs-1001-trop", "Troponin I", 0.9, "ng/mL", "HH")],
        "MedicationRequest": [_med("med-1001-asa", "aspirin")],
        "Condition": [_cond("cond-1001", "NSTEMI")],
    },
}


class _FakeFhir:
    """Async-context FHIR double: ``search`` over a cohort, ``read`` by id.

    ``read`` indexes a (possibly drifted) copy so a test can simulate the live
    record diverging from what the agent answered against.
    """

    def __init__(
        self,
        cohort: dict[str, dict[str, list[dict[str, Any]]]],
        read_cohort: dict[str, dict[str, list[dict[str, Any]]]] | None = None,
    ) -> None:
        self._cohort = cohort
        index_source = read_cohort if read_cohort is not None else cohort
        self._by_id: dict[tuple[str, str], dict[str, Any]] = {}
        for bytype in index_source.values():
            for rtype, rlist in bytype.items():
                for r in rlist:
                    self._by_id[(rtype, r["id"])] = r

    async def __aenter__(self) -> _FakeFhir:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def search(self, rtype: ResourceType, params: dict[str, str]) -> dict[str, Any]:
        pid = params.get("patient", "")
        resources = self._cohort.get(pid, {}).get(rtype.value, [])
        return {
            "resourceType": "Bundle",
            "type": "searchset",
            "total": len(resources),
            "entry": [{"resource": r} for r in resources],
        }

    async def read(self, rtype: ResourceType, rid: str) -> dict[str, Any]:
        res = self._by_id.get((rtype.value, rid))
        if res is None:
            raise RuntimeError(f"no such resource {rtype.value}/{rid}")
        return res


# --- observability spy -----------------------------------------------------


class _RecordingSpan:
    """Span double — ignores attributes/output; we only assert span names."""

    def set_attribute(self, key: str, value: Any) -> None:
        return None

    def set_output(self, value: Any) -> None:
        return None


class _RecordingObservability:
    """Records the spans opened and the verification results emitted."""

    def __init__(self) -> None:
        self.spans: list[str] = []
        self.verifications: list[dict[str, Any]] = []
        self.flushed = False

    @asynccontextmanager
    async def span(self, name: str, **attributes: Any) -> AsyncIterator[_RecordingSpan]:
        self.spans.append(name)
        yield _RecordingSpan()

    def event(self, name: str, **attributes: Any) -> None:
        return None

    def record_verification(self, *, passed: bool, action: str, patient_id: int) -> None:
        self.verifications.append({"passed": passed, "action": action, "patient_id": patient_id})

    def record_poller_staleness(self, *, patient_id: int, age_seconds: int) -> None:
        return None

    async def flush(self) -> None:
        self.flushed = True


def _client_with_observability(spy: _RecordingObservability) -> TestClient:
    """A client whose app publishes ``spy`` as its observability backend."""
    from copilot.api.app import create_app
    from copilot.config import get_settings
    from copilot.memory.db import get_engine, get_session_factory

    get_engine.cache_clear()
    get_session_factory.cache_clear()
    app = create_app(get_settings(), probe_factories=[])
    app.state.observability = spy
    return TestClient(app)


# --- fixtures --------------------------------------------------------------


@pytest.fixture
def _db_file(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Point Settings at a temp SQLite file and create the schema."""
    from copilot.config import get_settings
    from copilot.memory.db import Base, get_engine, get_session_factory

    db_file = tmp_path / "chat.db"
    monkeypatch.setenv("COPILOT_DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
    monkeypatch.setenv("COPILOT_FHIR_BASE_URL", "http://oe.test/fhir")
    monkeypatch.setenv("COPILOT_ANTHROPIC_API_KEY", "")  # -> deterministic stub agent
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
    """Replace the service's FHIR reader with the in-memory cohort double."""
    monkeypatch.setattr(ChatService, "_fhir_client", lambda self: _FakeFhir(_COHORT))


@pytest.fixture(autouse=True)
def _authorize_clinician(_db_file: str) -> None:
    """Seed a rounding cursor so CLIN is authorized for the cohort patients.

    Chat now enforces the rounding-list authorization boundary (UC-6): a clinician
    may only chat about patients on their established rounding list. These unit
    tests inject FHIR only into ChatService (not RoundsService), so we authorize by
    seeding the cursor directly rather than driving ``POST /v1/rounds/start``.
    """
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
    # The seed ran on its own event loop; drop the cached engine so the request
    # loop (via TestClient) gets a fresh one. _client() also clears these.
    get_engine.cache_clear()
    get_session_factory.cache_clear()


def _client() -> TestClient:
    from copilot.api.app import create_app
    from copilot.config import get_settings
    from copilot.memory.db import get_engine, get_session_factory

    get_engine.cache_clear()
    get_session_factory.cache_clear()
    return TestClient(create_app(get_settings(), probe_factories=[]))


def _chat(
    client: TestClient,
    message: str,
    *,
    patient_id: int = SICK,
    conversation_id: int | None = None,
    correlation_id: str | None = None,
) -> Any:
    body: dict[str, Any] = {"clinician_id": CLIN, "patient_id": patient_id, "message": message}
    if conversation_id is not None:
        body["conversation_id"] = conversation_id
    if correlation_id is not None:
        body["correlation_id"] = correlation_id
    return client.post("/v1/chat", json=body)


# --- tests -----------------------------------------------------------------


class TestChat:
    def test_present_data_is_served_and_grounded(self, _db_file: str) -> None:
        client = _client()
        r = _chat(client, "What is the latest troponin value?")
        assert r.status_code == 200
        body = r.json()
        assert body["verification"]["action"] == "served"
        assert body["verification"]["passed"] is True
        assert body["claims"], "a served answer must carry grounded claims"
        for claim in body["claims"]:
            ref = claim["source_ref"]
            assert set(ref) >= {"resource_type", "resource_id", "field", "value"}
        values = {c["source_ref"]["value"] for c in body["claims"]}
        assert "0.9" in values

    def test_absent_data_is_withheld_gracefully(self, _db_file: str) -> None:
        client = _client()
        r = _chat(client, "What did the patient's MRI brain show?")
        assert r.status_code == 200
        body = r.json()
        assert body["verification"]["action"] == "withheld"
        assert body["verification"]["passed"] is False
        assert body["claims"] == []
        assert body["answer"], "a withheld answer must still say something honest"

    def test_drifted_record_is_withheld_fail_closed(
        self, _db_file: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Agent answered against 0.9, but the live re-fetch now reads 1.5 → withhold."""
        drifted = copy.deepcopy(_COHORT)
        drifted["1001"]["Observation"][0]["valueQuantity"]["value"] = 1.5
        monkeypatch.setattr(
            ChatService, "_fhir_client", lambda self: _FakeFhir(_COHORT, read_cohort=drifted)
        )
        client = _client()
        r = _chat(client, "What is the latest troponin value?")
        assert r.status_code == 200
        body = r.json()
        assert body["verification"]["action"] == "withheld"
        assert body["claims"] == []

    def test_multiturn_conversation_persisted(self, _db_file: str) -> None:
        client = _client()
        first = _chat(client, "What is the latest troponin?")
        assert first.status_code == 200
        conv_id = first.json()["conversation_id"]
        assert isinstance(conv_id, int)

        second = _chat(client, "And is she on aspirin?", conversation_id=conv_id)
        assert second.status_code == 200
        assert second.json()["conversation_id"] == conv_id

        hist = client.get(f"/v1/conversations/{conv_id}")
        assert hist.status_code == 200
        messages = hist.json()["messages"]
        assert [m["role"] for m in messages] == ["user", "assistant", "user", "assistant"]
        assert messages[0]["content"] == "What is the latest troponin?"
        assert messages[2]["content"] == "And is she on aspirin?"

    def test_correlation_id_echoed_when_valid(self, _db_file: str) -> None:
        client = _client()
        cid = "chat-corr-12345678"
        r = _chat(client, "What is the latest troponin?", correlation_id=cid)
        assert r.status_code == 200
        assert r.json()["correlation_id"] == cid

    def test_invalid_correlation_id_is_replaced(self, _db_file: str) -> None:
        client = _client()
        r = _chat(client, "What is the latest troponin?", correlation_id="short")
        assert r.status_code == 200
        echoed = r.json()["correlation_id"]
        assert echoed != "short"
        assert len(echoed) >= 8

    def test_conversation_id_is_stable_and_new_per_thread(self, _db_file: str) -> None:
        client = _client()
        a = _chat(client, "What is the latest troponin?").json()["conversation_id"]
        b = _chat(client, "What is the latest troponin?").json()["conversation_id"]
        assert a != b, "each fresh chat (no conversation_id) opens a new thread"

    def test_unknown_conversation_reads_empty(self, _db_file: str) -> None:
        client = _client()
        r = client.get("/v1/conversations/999999")
        assert r.status_code == 200
        assert r.json() == {"messages": []}

    def test_served_chat_emits_span_and_verification(self, _db_file: str) -> None:
        spy = _RecordingObservability()
        client = _client_with_observability(spy)
        r = _chat(client, "What is the latest troponin value?")
        assert r.status_code == 200
        assert r.json()["verification"]["action"] == "served"
        assert "chat" in spy.spans
        assert spy.verifications == [{"passed": True, "action": "served", "patient_id": SICK}]

    def test_withheld_chat_records_failed_verification(self, _db_file: str) -> None:
        spy = _RecordingObservability()
        client = _client_with_observability(spy)
        r = _chat(client, "What did the patient's MRI brain show?")
        assert r.status_code == 200
        assert r.json()["verification"]["action"] == "withheld"
        assert spy.verifications == [{"passed": False, "action": "withheld", "patient_id": SICK}]
