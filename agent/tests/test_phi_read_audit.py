"""HIPAA §164.312(b) access-trail coverage for the PHI-read routes (Round-4 audit).

Four PHI-disclosing reads each ran a successful authorization check and then
returned protected health information **without writing an ``audit_log`` row**,
unlike every sibling read (``rounds`` start/current/advance/jump,
``observations.series``, ``rounds.refresh``). Broad clinical-data access in this
system is accepted *only* because every access is audited, so an unaudited
disclosure punches a hole in exactly that compensating control:

* ``GET /v1/documents/{id}``            → ``action="document.read"``
* ``GET /v1/documents/{id}/pages/{n}``  → ``action="document.page.read"``
* ``GET /v1/conversations/{id}``        → ``action="conversation.read"``
* ``GET /v1/rounds/alerts``             → ``action="rounds.alerts"`` (per returned patient)

Each new row is fail-open (own transaction, exception logged and swallowed) so a
failed audit write can never turn a served read into a 500 — mirroring
``observations._record_read_audit`` and ``RoundsService._record_reads_audit``.

This file ALSO guards the P3 ordering defect on ``GET /v1/conversations/{id}``:
the transcript (PHI) was loaded from the store *before* the authorization check,
the same PHI-load-before-authz pattern already closed on the document reads. An
unauthorized read must refuse (404) without loading a single message.

Drives the real FastAPI app + repository against a temp-file SQLite DB, reading
``audit_log`` back with a plain sync connection — the same shape as
``test_audit.py`` / ``test_refresh_audit.py``. Append-only; RED on pre-fix code,
GREEN after.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from typing import Any

import anyio
import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from copilot.domain.primitives import ResourceType
from copilot.memory.repository import MemoryRepository
from copilot.rounds.service import RoundsService

CLIN = 9101
PATIENT = 5101
# Rounds-alerts cohort: a critical (HH) troponin scores in the 8.0-10.0 acuity
# band, above the 7.0 alert threshold, so this patient surfaces as an alert.
SICK = 1001
STRANGER = 9199  # a clinician with no round on PATIENT — never authorized

_MESSAGES = [
    ("user", "What changed for this patient overnight?"),
    ("assistant", "Troponin rose to 0.9 ng/mL (critical)."),
]


# --- synthetic FHIR double (only the alerts route's rounds/start reaches it) --


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


_COHORT: dict[str, dict[str, list[dict[str, Any]]]] = {
    "1001": {"Observation": [_obs("obs-1001-trop", "Troponin I", 0.9, "ng/mL", "HH")]},
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
        resources = self._cohort.get(params.get("patient", ""), {}).get(rtype.value, [])
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
    """Point Settings at a temp SQLite file and create the schema (disabled mode)."""
    from copilot.config import get_settings
    from copilot.memory.db import Base, get_engine, get_session_factory

    db_file = tmp_path / "phi_read_audit.db"
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
    """Replace the rounds service's FHIR reader with the in-memory cohort double.

    Only the alerts test (which first runs rounds/start to build the memory file)
    reaches it; the document/conversation reads never touch FHIR.
    """
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
            "SELECT action, patient_id, clinician_id, correlation_id FROM audit_log"
        )
        cols = ("action", "patient_id", "clinician_id", "correlation_id")
        return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]
    finally:
        con.close()


# --- seed helpers (each does ALL its DB work in ONE event loop) --------------
# The async engine is bound to the loop that created it, so every distinct
# anyio.run must build on a freshly-cleared cache; combining a test's seeding
# into a single coroutine keeps it to one loop, and _client() clears the caches
# again before the TestClient's own loop opens.


async def _seed_document_and_authorize(patient_id: int, clinician_id: int) -> int:
    """Insert one extracted document (with a page + fact) and put its patient on
    the clinician's rounding list, so the read authorizes."""
    from copilot.domain.primitives import ClinicianId
    from copilot.memory.db import session_scope

    async with session_scope() as session:
        repo = MemoryRepository(session)
        doc = await repo.create_source_document(
            patient_id=patient_id,
            doc_type="lab_pdf",
            correlation_id="c-phi",
            openemr_document_id="oe-phi",
            content_hash="hash-phi",
            page_count=1,
            status="extracted",
        )
        await repo.create_document_page(
            source_document_id=doc.id, page_no=1, width=10, height=10, image=b"png-bytes"
        )
        extraction = await repo.create_extraction(
            source_document_id=doc.id,
            correlation_id="c-phi",
            schema_version="w2-v1",
            model="stub",
            status="ok",
        )
        await repo.create_extracted_fact(
            extraction_id=extraction.id, field_path="hemoglobin", value="13.5"
        )
        await repo.upsert_rounding_cursor(ClinicianId(value=clinician_id), [patient_id], 0, [])
        return int(doc.id)


async def _seed_conversation(
    clinician_id: int, patient_id: int, messages: list[tuple[str, str]], authorize_for: int
) -> int:
    """Open a conversation and authorize ``authorize_for`` (a *clinician* id) on its patient.

    Pass the reader you want authorized, or a clinician who has no round on
    ``patient_id`` to leave the reader off-round. Positional (``anyio.run`` forwards
    positional args only).
    """
    from copilot.domain.primitives import ClinicianId, PatientId
    from copilot.memory.db import session_scope

    async with session_scope() as session:
        repo = MemoryRepository(session)
        cid = await repo.create_conversation(
            ClinicianId(value=clinician_id), PatientId(value=patient_id), "c-phi-conv"
        )
        for role, content in messages:
            await repo.append_message(cid, role, content)
        await repo.upsert_rounding_cursor(ClinicianId(value=authorize_for), [patient_id], 0, [])
        return cid


def _start(client: TestClient, ids: list[int]) -> Any:
    return client.post("/v1/rounds/start", json={"clinician_id": CLIN, "patient_ids": ids})


# --- P2: each PHI read writes an audit row ----------------------------------


class TestDocumentReadAudit:
    def test_get_document_read_writes_audit_row(self, _db_file: str) -> None:
        doc_id = anyio.run(_seed_document_and_authorize, PATIENT, CLIN)
        client = _client()

        r = client.get(f"/v1/documents/{doc_id}", params={"clinician_id": CLIN})
        assert r.status_code == 200, f"authorized document read must serve, got {r.status_code}"

        rows = [x for x in _audit_rows(_db_file) if x["action"] == "document.read"]
        assert rows, "an authorized document read must write a document.read audit row"
        assert any(
            x["patient_id"] == PATIENT and x["clinician_id"] == CLIN for x in rows
        ), "the document.read row must name the patient and the clinician"

    def test_get_document_page_read_writes_audit_row(self, _db_file: str) -> None:
        doc_id = anyio.run(_seed_document_and_authorize, PATIENT, CLIN)
        client = _client()

        r = client.get(f"/v1/documents/{doc_id}/pages/1", params={"clinician_id": CLIN})
        assert r.status_code == 200, f"authorized page read must serve, got {r.status_code}"

        rows = [x for x in _audit_rows(_db_file) if x["action"] == "document.page.read"]
        assert rows, "an authorized page read must write a document.page.read audit row"
        assert any(
            x["patient_id"] == PATIENT and x["clinician_id"] == CLIN for x in rows
        ), "the document.page.read row must name the patient and the clinician"

    def test_document_read_audit_failure_does_not_break_the_read(
        self, _db_file: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fail-open: a failed audit write must never 500 an authorized read."""
        doc_id = anyio.run(_seed_document_and_authorize, PATIENT, CLIN)

        async def _boom(self: MemoryRepository, **_kwargs: Any) -> None:
            raise RuntimeError("audit backend down")

        monkeypatch.setattr(MemoryRepository, "record_audit", _boom)

        client = _client()
        r = client.get(f"/v1/documents/{doc_id}", params={"clinician_id": CLIN})

        assert r.status_code == 200, "a failed audit write must not break the served read"
        # The write raised and was swallowed -> no row committed.
        assert not [x for x in _audit_rows(_db_file) if x["action"] == "document.read"]


class TestConversationReadAudit:
    def test_get_conversation_read_writes_audit_row(self, _db_file: str) -> None:
        conv_id = anyio.run(_seed_conversation, CLIN, PATIENT, _MESSAGES, CLIN)
        client = _client()

        r = client.get(f"/v1/conversations/{conv_id}", params={"clinician_id": CLIN})
        assert r.status_code == 200, f"authorized conversation read must serve, got {r.status_code}"

        rows = [x for x in _audit_rows(_db_file) if x["action"] == "conversation.read"]
        assert rows, "an authorized conversation read must write a conversation.read audit row"
        assert any(
            x["patient_id"] == PATIENT and x["clinician_id"] == CLIN for x in rows
        ), "the conversation.read row must name the patient and the clinician"

    def test_conversation_read_audit_failure_does_not_break_the_read(
        self, _db_file: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fail-open: a failed audit write must never 500 the transcript read."""
        conv_id = anyio.run(_seed_conversation, CLIN, PATIENT, _MESSAGES, CLIN)

        async def _boom(self: MemoryRepository, **_kwargs: Any) -> None:
            raise RuntimeError("audit backend down")

        monkeypatch.setattr(MemoryRepository, "record_audit", _boom)

        client = _client()
        r = client.get(f"/v1/conversations/{conv_id}", params={"clinician_id": CLIN})

        assert r.status_code == 200, "a failed audit write must not break the served read"
        assert not [x for x in _audit_rows(_db_file) if x["action"] == "conversation.read"]


class TestRoundsAlertsAudit:
    def test_alerts_read_writes_an_audit_row_per_returned_patient(self, _db_file: str) -> None:
        client = _client()
        # rounds/start builds the memory file (critical troponin -> high acuity) and
        # the cursor; nobody is last_seen yet, so the patient surfaces as an alert.
        assert _start(client, [SICK]).status_code == 200

        r = client.get("/v1/rounds/alerts", params={"clinician_id": CLIN})
        assert r.status_code == 200
        alerts = r.json()["alerts"]
        assert alerts, "the critical patient must surface as a deterioration alert"

        rows = [x for x in _audit_rows(_db_file) if x["action"] == "rounds.alerts"]
        assert rows, "an alerts read that discloses patients must write a rounds.alerts audit row"
        assert any(
            x["patient_id"] == SICK and x["clinician_id"] == CLIN for x in rows
        ), "the rounds.alerts row must name the disclosed patient and the clinician"

    def test_alerts_audit_failure_does_not_break_the_read(
        self, _db_file: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fail-open: a failed audit write must never 500 the alerts read."""
        client = _client()
        assert _start(client, [SICK]).status_code == 200

        async def _boom(self: MemoryRepository, **_kwargs: Any) -> None:
            raise RuntimeError("audit backend down")

        monkeypatch.setattr(MemoryRepository, "record_audit", _boom)

        r = client.get("/v1/rounds/alerts", params={"clinician_id": CLIN})
        assert r.status_code == 200, "a failed audit write must not break the alerts read"
        assert r.json()["alerts"], "the alert must still be served"
        assert not [x for x in _audit_rows(_db_file) if x["action"] == "rounds.alerts"]


# --- P3: conversation read authorizes BEFORE loading the transcript ---------


class TestConversationReadOrdering:
    def test_unauthorized_conversation_read_loads_no_messages(
        self, _db_file: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """P3: the refusal must precede the transcript load, not follow it.

        Loading ``get_conversation_messages`` before ``is_authorized`` meant an
        off-round caller had the conversation's PHI pulled from the store on their
        behalf (and made the 404's latency scale with the thread's size). Authorize
        first, then load — the same discipline the document reads already keep.
        """
        # Owner CLIN's thread about PATIENT; STRANGER is never put on PATIENT's round.
        conv_id = anyio.run(_seed_conversation, CLIN, PATIENT, _MESSAGES, CLIN)

        loaded: list[int] = []
        original = MemoryRepository.get_conversation_messages

        async def _spy(self: MemoryRepository, conversation_id: int) -> Any:
            loaded.append(conversation_id)
            return await original(self, conversation_id)

        monkeypatch.setattr(MemoryRepository, "get_conversation_messages", _spy)

        client = _client()
        r = client.get(f"/v1/conversations/{conv_id}", params={"clinician_id": STRANGER})

        assert r.status_code == 404, (
            f"an off-round clinician must get an existence-hiding 404, got {r.status_code}"
        )
        assert loaded == [], (
            "an off-round caller must not cause the transcript to be loaded "
            "(PHI-before-authz read)"
        )
