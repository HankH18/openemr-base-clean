"""Audit-trail feature tests — every PHI read writes an append-only audit row.

Drives the real FastAPI app + repository against a temp-file SQLite DB, with the
FHIR reader replaced by an in-memory cohort double (monkeypatching each service's
``_fhir_client``) — the same pattern as ``test_chat_routes`` / ``test_rounds_routes``.
After a read, the ``audit_log`` table is read back with a plain sync connection
(no cross-event-loop async surprises) and asserted on.

record_audit already exists but was never called; these prove the read paths now
invoke it — one row per chat PHI read and one per patient chart a round reads.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from copilot.chat.service import ChatService
from copilot.domain.primitives import ResourceType
from copilot.rounds.service import RoundsService

CLIN = 9001
SICK = 1001
WARN = 1002


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


# 1001: NSTEMI + critical troponin + aspirin (chat grounds against this).
# 1002: a warning potassium (a second chart for rounds/start to read).
_COHORT: dict[str, dict[str, list[dict[str, Any]]]] = {
    "1001": {
        "Observation": [_obs("obs-1001-trop", "Troponin I", 0.9, "ng/mL", "HH")],
        "MedicationRequest": [_med("med-1001-asa", "aspirin")],
        "Condition": [_cond("cond-1001", "NSTEMI")],
    },
    "1002": {
        "Observation": [_obs("obs-1002-k", "Potassium", 5.6, "mmol/L", "H")],
    },
}


class _FakeFhir:
    """Async-context FHIR double: ``search`` over a cohort, ``read`` by id."""

    def __init__(self, cohort: dict[str, dict[str, list[dict[str, Any]]]]) -> None:
        self._cohort = cohort
        self._by_id: dict[tuple[str, str], dict[str, Any]] = {}
        for bytype in cohort.values():
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


# --- fixtures --------------------------------------------------------------


@pytest.fixture
def _db_file(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Point Settings at a temp SQLite file and create the schema."""
    from copilot.config import get_settings
    from copilot.memory.db import Base, get_engine, get_session_factory

    db_file = tmp_path / "audit.db"
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
    """Replace both services' FHIR readers with the in-memory cohort double."""
    monkeypatch.setattr(ChatService, "_fhir_client", lambda self: _FakeFhir(_COHORT))
    monkeypatch.setattr(RoundsService, "_fhir_client", lambda self: _FakeFhir(_COHORT))


def _client() -> TestClient:
    from copilot.api.app import create_app
    from copilot.config import get_settings
    from copilot.memory.db import get_engine, get_session_factory

    get_engine.cache_clear()
    get_session_factory.cache_clear()
    return TestClient(create_app(get_settings(), probe_factories=[]))


def _audit_rows(db_file: str) -> list[dict[str, Any]]:
    """Read the audit_log table back with a plain sync connection."""
    con = sqlite3.connect(db_file)
    try:
        cur = con.execute(
            "SELECT action, patient_id, clinician_id, correlation_id, resources_returned "
            "FROM audit_log"
        )
        cols = ("action", "patient_id", "clinician_id", "correlation_id", "resources_returned")
        return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]
    finally:
        con.close()


def _start(client: TestClient, ids: list[int]) -> Any:
    return client.post("/v1/rounds/start", json={"clinician_id": CLIN, "patient_ids": ids})


def _chat(client: TestClient, message: str, *, patient_id: int = SICK) -> Any:
    return client.post(
        "/v1/chat",
        json={"clinician_id": CLIN, "patient_id": patient_id, "message": message},
    )


# --- tests -----------------------------------------------------------------


class TestChatAudit:
    def test_chat_read_writes_an_audit_row(self, _db_file: str) -> None:
        client = _client()
        assert _start(client, [SICK]).status_code == 200
        assert _chat(client, "What is the latest troponin value?").status_code == 200

        rows = _audit_rows(_db_file)
        chat_rows = [r for r in rows if r["action"] == "chat"]
        assert chat_rows, "a chat PHI read must invoke record_audit"
        assert any(
            r["patient_id"] == SICK and r["clinician_id"] == CLIN for r in chat_rows
        ), "the chat audit row must name the patient and the clinician"

    def test_chat_audit_row_records_cited_resources(self, _db_file: str) -> None:
        client = _client()
        _start(client, [SICK])
        assert _chat(client, "What is the latest troponin value?").status_code == 200

        chat_rows = [r for r in _audit_rows(_db_file) if r["action"] == "chat"]
        assert chat_rows
        # resources_returned is JSON-encoded in SQLite; a served troponin answer
        # cites the observation it grounded against.
        assert any("obs-1001-trop" in (r["resources_returned"] or "") for r in chat_rows)


class TestRoundsAudit:
    def test_rounds_start_audits_each_patient_read(self, _db_file: str) -> None:
        client = _client()
        assert _start(client, [SICK, WARN]).status_code == 200

        start_rows = [r for r in _audit_rows(_db_file) if r["action"] == "rounds.start"]
        patients = {r["patient_id"] for r in start_rows}
        assert {SICK, WARN} <= patients, "rounds/start must audit every chart it read"
        assert all(r["clinician_id"] == CLIN for r in start_rows)

    def test_current_advance_jump_audit_the_served_card(self, _db_file: str) -> None:
        client = _client()
        _start(client, [SICK, WARN])

        assert (
            client.get("/v1/rounds/current", params={"clinician_id": CLIN}).status_code == 200
        )
        assert (
            client.post(
                "/v1/rounds/advance",
                json={"clinician_id": CLIN, "completed_patient_id": SICK},
            ).status_code
            == 200
        )
        assert (
            client.post(
                "/v1/rounds/jump", json={"clinician_id": CLIN, "patient_id": SICK}
            ).status_code
            == 200
        )

        actions = {r["action"] for r in _audit_rows(_db_file)}
        assert {"rounds.current", "rounds.advance", "rounds.jump"} <= actions


class TestAuditCorrelation:
    def test_every_audit_row_carries_a_non_empty_correlation_id(self, _db_file: str) -> None:
        client = _client()
        _start(client, [SICK, WARN])
        _chat(client, "What is the latest troponin value?")

        rows = _audit_rows(_db_file)
        assert rows, "expected audit rows after a round + chat"
        assert all(
            isinstance(r["correlation_id"], str) and r["correlation_id"] for r in rows
        ), "every audit row must record the request correlation id"
