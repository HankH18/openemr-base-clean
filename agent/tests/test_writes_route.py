"""Write-back route tests — the propose→confirm gate, authorized + fail-closed.

Drives the real FastAPI app + repository against a temp-file SQLite DB, with the
service's write client and read-back client replaced by in-memory doubles
(monkeypatching ``WriteService._write_client`` / ``._read_client``). No network is
touched and no real write credentials are needed — ``build_write_client`` is never
called. Write-back is enabled via env for the write tests and explicitly disabled
for the disabled-flag test.

Covered: propose→confirm happy path; the rounding-list 403 (no audit); the
``write_proposed`` + ``write_committed`` audit rows carrying ``entry_mode`` /
clinician / resource id; audit fail-open (a broken audit never 500s a served
write); double-confirm idempotency (one write, replayed proof); an out-of-range
human_direct value warning that still commits; unparseable / wrong-unit → 400;
and the disabled flag → 503 with no write attempted.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from copilot.domain.primitives import ClinicianId, PatientId, ResourceType, utcnow
from copilot.domain.writes import (
    CommittedWrite,
    VitalWrite,
    WritableMetric,
    WriteCandidate,
    WriteKind,
)
from copilot.writeback.service import WriteService, get_idempotency_store

CLIN = 9001
PID = 1015
UNAUTH_CLIN = 9999


# --- write / read doubles ---------------------------------------------------


class _FakeWriter:
    """Async-context write-client double. Records every append for assertions."""

    def __init__(self) -> None:
        self.vitals: list[dict[str, Any]] = []
        self.meds: list[dict[str, Any]] = []
        self.encounters_resolved = 0

    async def __aenter__(self) -> _FakeWriter:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def resolve_or_create_encounter(self, pid: PatientId) -> str:
        self.encounters_resolved += 1
        return "42"

    async def create_vital(
        self, pid: PatientId, eid: str, vital: VitalWrite, *, idempotency_key: str | None = None
    ) -> CommittedWrite:
        self.vitals.append({"pid": pid.value, "eid": eid, "vital": vital, "key": idempotency_key})
        return CommittedWrite(
            resource_kind=WriteKind.vital,
            new_id="vid-555",
            encounter_id=str(eid),
            committed_at=utcnow(),
        )

    async def create_medication(
        self, pid: PatientId, med: Any, *, idempotency_key: str | None = None
    ) -> CommittedWrite:
        self.meds.append({"pid": pid.value, "med": med, "key": idempotency_key})
        return CommittedWrite(
            resource_kind=WriteKind.medication,
            new_id="med-900",
            encounter_id=None,
            committed_at=utcnow(),
        )


class _FakeReader:
    """Read-back double — empty bundle exercises the fail-open log-only path."""

    async def __aenter__(self) -> _FakeReader:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def search(self, rtype: ResourceType, params: dict[str, str]) -> dict[str, Any]:
        return {"resourceType": "Bundle", "type": "searchset", "total": 0, "entry": []}


# --- fixtures --------------------------------------------------------------


@pytest.fixture
def _db_file(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Point Settings at a temp SQLite file, enable write-back, create the schema."""
    from copilot.config import get_settings
    from copilot.memory.db import Base, get_engine, get_session_factory

    db_file = tmp_path / "writes.db"
    monkeypatch.setenv("COPILOT_DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
    monkeypatch.setenv("COPILOT_FHIR_BASE_URL", "http://oe.test/fhir")
    monkeypatch.setenv("COPILOT_WRITEBACK_ENABLED", "true")
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()
    get_idempotency_store.cache_clear()

    import copilot.memory.models  # noqa: F401  (registers tables on Base.metadata)

    sync_engine = sa.create_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()
    yield str(db_file)
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()
    get_idempotency_store.cache_clear()


@pytest.fixture(autouse=True)
def writer(monkeypatch: pytest.MonkeyPatch) -> _FakeWriter:
    """Replace the write + read-back clients with in-memory doubles.

    Autouse so no test accidentally builds the guarded real write client; also
    requestable by name to assert on the recorded appends.
    """
    fake = _FakeWriter()
    monkeypatch.setattr(WriteService, "_write_client", lambda self: fake)
    monkeypatch.setattr(WriteService, "_read_client", lambda self: _FakeReader())
    return fake


@pytest.fixture(autouse=True)
def _authorize_clinician(_db_file: str) -> None:
    """Seed a rounding cursor so CLIN is authorized for PID (UC-6 boundary)."""
    from copilot.memory.db import get_engine, get_session_factory, session_scope
    from copilot.memory.repository import MemoryRepository

    async def _seed() -> None:
        async with session_scope() as session:
            await MemoryRepository(session).upsert_rounding_cursor(
                ClinicianId(value=CLIN), [PID], 0, []
            )

    asyncio.run(_seed())
    get_engine.cache_clear()
    get_session_factory.cache_clear()


def _client() -> TestClient:
    from copilot.api.app import create_app
    from copilot.config import get_settings
    from copilot.memory.db import get_engine, get_session_factory

    get_engine.cache_clear()
    get_session_factory.cache_clear()
    return TestClient(create_app(get_settings(), probe_factories=[]))


def _propose(
    client: TestClient,
    *,
    clinician_id: int = CLIN,
    patient_id: int = PID,
    kind: str = "vital",
    metric: str | None = "heart_rate",
    raw_value: str = "72",
    unit: str | None = "bpm",
) -> Any:
    body: dict[str, Any] = {
        "clinician_id": clinician_id,
        "patient_id": patient_id,
        "kind": kind,
        "raw_value": raw_value,
    }
    if metric is not None:
        body["metric"] = metric
    if unit is not None:
        body["unit"] = unit
    return client.post("/v1/writes", json=body)


def _confirm(client: TestClient, key: str, candidate: dict[str, Any]) -> Any:
    return client.post(f"/v1/writes/{key}/confirm", json={"candidate": candidate})


def _audit_rows(db_file: str) -> list[dict[str, Any]]:
    con = sqlite3.connect(db_file)
    try:
        cur = con.execute(
            "SELECT action, patient_id, clinician_id, entry_mode, resources_returned "
            "FROM audit_log"
        )
        cols = ("action", "patient_id", "clinician_id", "entry_mode", "resources_returned")
        return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]
    finally:
        con.close()


# --- tests -----------------------------------------------------------------


class TestProposeConfirmHappyPath:
    def test_propose_then_confirm_commits_append_only(
        self, _db_file: str, writer: _FakeWriter
    ) -> None:
        client = _client()

        proposed = _propose(client)
        assert proposed.status_code == 200
        body = proposed.json()
        # Structured echo-back — the exact record + the append-only notice.
        assert body["candidate"]["kind"] == "vital"
        assert body["candidate"]["vital"]["metric"] == "heart_rate"
        assert body["candidate"]["vital"]["value"] == 72.0
        assert body["candidate"]["vital"]["unit"] == "bpm"
        assert body["effective_time"] == "now"
        assert "does not overwrite" in body["notice"]
        assert body["warnings"] == []  # in-range value ⇒ no soft warning
        key = body["idempotency_key"]
        assert key and body["candidate"]["idempotency_key"] == key

        # Nothing has been written yet — propose never touches OpenEMR.
        assert writer.vitals == []

        confirmed = _confirm(client, key, body["candidate"])
        assert confirmed.status_code == 200
        committed = confirmed.json()
        assert committed["resource_kind"] == "vital"
        assert committed["new_id"] == "vid-555"
        assert committed["encounter_id"] == "42"

        # Exactly one append, through an encounter it resolved first.
        assert len(writer.vitals) == 1
        assert writer.encounters_resolved == 1
        assert writer.vitals[0]["vital"].metric is WritableMetric.heart_rate
        assert writer.vitals[0]["key"] == key


class TestAuthorization:
    def test_unauthorized_clinician_is_403_with_no_audit(self, _db_file: str) -> None:
        client = _client()
        r = _propose(client, clinician_id=UNAUTH_CLIN)
        assert r.status_code == 403
        # A refused write took no PHI action, so it leaves no trail.
        assert _audit_rows(_db_file) == []


class TestAudit:
    def test_proposed_and_committed_rows_carry_entry_mode_and_ids(
        self, _db_file: str
    ) -> None:
        client = _client()
        body = _propose(client).json()
        assert _confirm(client, body["idempotency_key"], body["candidate"]).status_code == 200

        rows = _audit_rows(_db_file)
        proposed = [r for r in rows if r["action"] == "write_proposed"]
        committed = [r for r in rows if r["action"] == "write_committed"]
        assert proposed, "propose must record a write_proposed audit row"
        assert committed, "confirm must record a write_committed audit row"

        for row in (*proposed, *committed):
            assert row["entry_mode"] == "human_direct"
            assert row["clinician_id"] == CLIN
            assert row["patient_id"] == PID

        # The committed row names the created resource (JSON-encoded in SQLite).
        assert "vid-555" in (committed[0]["resources_returned"] or "")

    def test_record_audit_failure_does_not_500(
        self, _db_file: str, monkeypatch: pytest.MonkeyPatch, writer: _FakeWriter
    ) -> None:
        """Fail-open: a broken audit write never turns a completed write into a 500."""
        from copilot.memory.repository import MemoryRepository

        async def _boom(self: Any, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError("audit write exploded")

        monkeypatch.setattr(MemoryRepository, "record_audit", _boom)

        client = _client()
        proposed = _propose(client)
        assert proposed.status_code == 200
        body = proposed.json()
        confirmed = _confirm(client, body["idempotency_key"], body["candidate"])
        assert confirmed.status_code == 200
        # The write still landed despite the audit blowing up.
        assert len(writer.vitals) == 1


class TestIdempotency:
    def test_double_confirm_same_key_is_idempotent(
        self, _db_file: str, writer: _FakeWriter
    ) -> None:
        client = _client()
        body = _propose(client).json()
        key, candidate = body["idempotency_key"], body["candidate"]

        first = _confirm(client, key, candidate)
        second = _confirm(client, key, candidate)
        assert first.status_code == 200
        assert second.status_code == 200
        # Same proof replayed, and only ONE actual write occurred.
        assert first.json()["new_id"] == second.json()["new_id"] == "vid-555"
        assert len(writer.vitals) == 1


class TestSoftRangeWarning:
    def test_out_of_range_human_direct_warns_but_still_commits(
        self, _db_file: str, writer: _FakeWriter
    ) -> None:
        client = _client()
        # 350 bpm is outside the plausibility band (10-300) - a soft, overridable
        # warning for a human direct-edit, never a hard block.
        proposed = _propose(client, raw_value="350")
        assert proposed.status_code == 200
        body = proposed.json()
        assert body["warnings"], "an out-of-range human_direct value must surface a warning"
        assert any("physiologic range" in w for w in body["warnings"])
        assert body["verdict"]["blocked"] is False

        confirmed = _confirm(client, body["idempotency_key"], body["candidate"])
        assert confirmed.status_code == 200
        assert len(writer.vitals) == 1


class TestBadCandidate:
    def test_unparseable_value_is_400(self, _db_file: str, writer: _FakeWriter) -> None:
        client = _client()
        r = _propose(client, raw_value="not-a-number")
        assert r.status_code == 400
        assert writer.vitals == []

    def test_wrong_unit_is_400(self, _db_file: str, writer: _FakeWriter) -> None:
        client = _client()
        # A heart rate in mmHg is a different quantity — hard block at propose.
        r = _propose(client, unit="mmHg")
        assert r.status_code == 400
        assert writer.vitals == []


class TestDisabledFlag:
    def test_propose_is_503_when_writeback_disabled(
        self, _db_file: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from copilot.config import get_settings

        monkeypatch.setenv("COPILOT_WRITEBACK_ENABLED", "false")
        get_settings.cache_clear()

        client = _client()
        r = _propose(client)
        assert r.status_code == 503

    def test_confirm_is_503_and_attempts_no_write_when_disabled(
        self, _db_file: str, monkeypatch: pytest.MonkeyPatch, writer: _FakeWriter
    ) -> None:
        from copilot.config import get_settings

        monkeypatch.setenv("COPILOT_WRITEBACK_ENABLED", "false")
        get_settings.cache_clear()

        candidate = WriteCandidate(
            kind=WriteKind.vital,
            patient_id=PatientId(value=PID),
            clinician_id=ClinicianId(value=CLIN),
            idempotency_key="k-disabled-123",
            vital=VitalWrite(metric=WritableMetric.heart_rate, value=72, unit="bpm"),
        )
        client = _client()
        r = _confirm(client, "k-disabled-123", candidate.model_dump(mode="json"))
        assert r.status_code == 503
        # The write client was never built, so nothing was attempted.
        assert writer.vitals == []
