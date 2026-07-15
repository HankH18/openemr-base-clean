"""Intake write-back bridge tests — categorized facts → propose, never commit.

Proves the F4b bridge (``copilot.writeback.intake_bridge``) turns a document's
categorized intake facts into PROPOSED write candidates through the EXISTING
propose→confirm gate, and that it is structurally incapable of committing:

- (a) ``allergy`` / ``medication`` / ``medical_problem`` facts become the matching
  ProposedWrites (verbatim value as title, agent entry mode, not blocked);
- (b) ``demographic`` / ``chief_complaint`` / ``family_history`` (and lab / None)
  facts are IGNORED — they have no ``lists`` home;
- (c) the bridge NEVER commits — the write client is patched to raise if built, so
  the propose path completing at all proves no OpenEMR write; only ``write_proposed``
  audit rows are recorded, never ``write_committed`` / ``write_failed``;
- (d) an unauthorized patient is refused at the route with 403 and no audit.

Plus the route respects the ``writeback_enabled`` gate (503 when off) and the
cross-patient guard (404 for a document that is not this patient's). Drives the
real repository against a temp-file SQLite DB; no network is touched.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from copilot.domain.primitives import ClinicianId, PatientId
from copilot.domain.writes import ProposedWrite, WriteEntryMode, WriteKind
from copilot.observability import NoopObservability
from copilot.writeback.intake_bridge import IntakeWritebackBridge
from copilot.writeback.service import WriteService, get_idempotency_store

CLIN = 7001
PID = 1205
UNAUTH_PID = 4040
OTHER_PID = 3030


# --- fixtures --------------------------------------------------------------


@pytest.fixture
def _db_file(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Point Settings at a temp SQLite file, enable write-back, create the schema."""
    from copilot.config import get_settings
    from copilot.memory.db import Base, get_engine, get_session_factory

    db_file = tmp_path / "bridge.db"
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
def _no_write_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make building a write/read client explode.

    The propose path never builds one, so the bridge completing is a structural
    proof it took no write path. If a bug (or a future edit) reached ``commit``,
    it would try to build the client and fail loudly here.
    """

    def _boom(self: WriteService) -> Any:
        raise AssertionError("the bridge propose path must never build a write client")

    monkeypatch.setattr(WriteService, "_write_client", _boom)
    monkeypatch.setattr(WriteService, "_read_client", _boom)


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


# --- helpers ---------------------------------------------------------------


async def _seed_document(
    facts: list[tuple[str | None, str | None]], *, patient_id: int = PID
) -> int:
    """Create a source document + one extraction + the given (category, value) facts."""
    from copilot.memory.db import session_scope
    from copilot.memory.repository import MemoryRepository

    async with session_scope() as session:
        repo = MemoryRepository(session)
        document = await repo.create_source_document(
            patient_id=patient_id, doc_type="intake_form", correlation_id="corr-bridge-seed"
        )
        extraction = await repo.create_extraction(
            source_document_id=document.id, correlation_id="corr-bridge-seed"
        )
        for index, (category, value) in enumerate(facts):
            await repo.create_extracted_fact(
                extraction_id=extraction.id,
                field_path=f"fact[{index}]",
                value=value,
                category=category,
                supported=True,
            )
        return document.id


def _seed_sync(facts: list[tuple[str | None, str | None]], *, patient_id: int = PID) -> int:
    """Seed a document in a throwaway loop, then reset the engine cache for the app loop."""
    from copilot.memory.db import get_engine, get_session_factory

    document_id = asyncio.run(_seed_document(facts, patient_id=patient_id))
    get_engine.cache_clear()
    get_session_factory.cache_clear()
    return document_id


def _bridge() -> IntakeWritebackBridge:
    from copilot.config import get_settings

    return IntakeWritebackBridge(WriteService(get_settings(), NoopObservability()))


def _title_of(candidate: Any) -> str:
    """Verbatim title of whichever payload the candidate carries (kind-agnostic)."""
    for payload in (
        getattr(candidate, "allergy", None),
        getattr(candidate, "medication", None),
        getattr(candidate, "medical_problem", None),
    ):
        if payload is not None:
            title: str = payload.title
            return title
    raise AssertionError("candidate carried no writable payload")


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


def _client() -> TestClient:
    from copilot.api.app import create_app
    from copilot.config import get_settings
    from copilot.memory.db import get_engine, get_session_factory

    get_engine.cache_clear()
    get_session_factory.cache_clear()
    return TestClient(create_app(get_settings(), probe_factories=[]))


def _post(
    client: TestClient, document_id: int, *, clinician_id: int = CLIN, patient_id: int = PID
) -> Any:
    return client.post(
        f"/v1/writes/propose-from-document/{document_id}",
        json={"clinician_id": clinician_id, "patient_id": patient_id},
    )


# --- (a) categorized facts become the right ProposedWrites -----------------


class TestCategorizedFactsBecomeProposals:
    async def test_allergy_medication_problem_facts_map_to_write_kinds(
        self, _db_file: str
    ) -> None:
        document_id = await _seed_document(
            [
                ("allergy", "Penicillin"),
                ("medication", "Lisinopril 10mg daily"),
                ("medical_problem", "Type 2 diabetes mellitus"),
            ]
        )

        proposals = await _bridge().propose_writes_from_document(
            document_id=document_id,
            acting_clinician=ClinicianId(value=CLIN),
            patient_id=PatientId(value=PID),
        )

        assert len(proposals) == 3
        assert all(isinstance(p, ProposedWrite) for p in proposals)

        by_kind = {p.candidate.kind: p for p in proposals}
        assert set(by_kind) == {
            WriteKind.allergy,
            WriteKind.medication,
            WriteKind.medical_problem,
        }

        # Verbatim value carried through as the title; today's begdate; agent mode;
        # not blocked; each carries an idempotency_key for the physician confirm.
        assert _title_of(by_kind[WriteKind.allergy].candidate) == "Penicillin"
        assert _title_of(by_kind[WriteKind.medication].candidate) == "Lisinopril 10mg daily"
        assert _title_of(by_kind[WriteKind.medical_problem].candidate) == "Type 2 diabetes mellitus"

        for proposed in proposals:
            assert (
                proposed.candidate.entry_mode
                is WriteEntryMode.agent_proposed_physician_confirmed
            )
            assert proposed.verdict.blocked is False
            assert proposed.candidate.idempotency_key


# --- (b) non-writable categories are ignored -------------------------------


class TestNonWritableFactsIgnored:
    async def test_demographic_chief_complaint_family_history_and_lab_are_ignored(
        self, _db_file: str
    ) -> None:
        document_id = await _seed_document(
            [
                ("demographic", "Jane Doe"),
                ("chief_complaint", "Chest pain"),
                ("family_history", "Father: MI at 60"),
                (None, "13.5"),  # a lab fact carries no intake category
            ]
        )

        proposals = await _bridge().propose_writes_from_document(
            document_id=document_id,
            acting_clinician=ClinicianId(value=CLIN),
            patient_id=PatientId(value=PID),
        )

        assert proposals == []

    async def test_only_writable_facts_survive_a_mixed_document(self, _db_file: str) -> None:
        document_id = await _seed_document(
            [
                ("demographic", "Jane Doe"),
                ("allergy", "Sulfa drugs"),
                ("family_history", "Mother: breast cancer"),
                ("medication", "Metformin 500mg"),
            ]
        )

        proposals = await _bridge().propose_writes_from_document(
            document_id=document_id,
            acting_clinician=ClinicianId(value=CLIN),
            patient_id=PatientId(value=PID),
        )

        assert {p.candidate.kind for p in proposals} == {
            WriteKind.allergy,
            WriteKind.medication,
        }


# --- (c) the bridge never commits ------------------------------------------


class TestBridgeNeverCommits:
    async def test_propose_records_only_write_proposed_audit_no_commit(
        self, _db_file: str
    ) -> None:
        document_id = await _seed_document(
            [("allergy", "Penicillin"), ("medical_problem", "Hypertension")]
        )

        proposals = await _bridge().propose_writes_from_document(
            document_id=document_id,
            acting_clinician=ClinicianId(value=CLIN),
            patient_id=PatientId(value=PID),
        )
        assert len(proposals) == 2

        rows = _audit_rows(_db_file)
        actions = [r["action"] for r in rows]
        # Exactly one write_proposed per proposal, and NOTHING committed/failed —
        # the write client (patched to raise) was never built.
        assert actions.count("write_proposed") == 2
        assert "write_committed" not in actions
        assert "write_failed" not in actions

        for row in rows:
            assert row["action"] == "write_proposed"
            assert row["entry_mode"] == "agent_proposed_physician_confirmed"
            assert row["clinician_id"] == CLIN
            assert row["patient_id"] == PID
            # A proposal creates no resource, so no id is ever named on the trail.
            assert row["resources_returned"] in ("[]", None)


# --- (d) unauthorized patient is refused -----------------------------------


class TestAuthorization:
    def test_unauthorized_patient_is_403_with_no_audit(self, _db_file: str) -> None:
        client = _client()
        # UNAUTH_PID is not on CLIN's rounding list → refused before any DB read.
        response = _post(client, document_id=1, patient_id=UNAUTH_PID)
        assert response.status_code == 403
        # A refused request took no PHI action, so it leaves no trail.
        assert _audit_rows(_db_file) == []


# --- route: gate + wiring + cross-patient guard ----------------------------


class TestRoute:
    def test_route_returns_proposals_and_writes_nothing(self, _db_file: str) -> None:
        document_id = _seed_sync(
            [
                ("allergy", "Peanuts"),
                ("medication", "Atorvastatin 20mg"),
                ("chief_complaint", "Cough"),  # ignored
            ]
        )
        client = _client()
        response = _post(client, document_id=document_id)
        assert response.status_code == 200

        body = response.json()
        assert body["document_id"] == document_id
        assert body["count"] == 2
        assert len(body["proposals"]) == 2
        for proposal in body["proposals"]:
            assert proposal["idempotency_key"]
            assert proposal["candidate"]["idempotency_key"] == proposal["idempotency_key"]
            assert proposal["candidate"]["entry_mode"] == "agent_proposed_physician_confirmed"
            assert proposal["effective_time"] == "now"
            assert "does not overwrite" in proposal["notice"]

        # Only proposals were audited — the route never committed.
        actions = [r["action"] for r in _audit_rows(_db_file)]
        assert actions.count("write_proposed") == 2
        assert "write_committed" not in actions

    def test_route_is_503_when_writeback_disabled(
        self, _db_file: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        document_id = _seed_sync([("allergy", "Penicillin")])
        from copilot.config import get_settings

        monkeypatch.setenv("COPILOT_WRITEBACK_ENABLED", "false")
        get_settings.cache_clear()

        client = _client()
        response = _post(client, document_id=document_id)
        assert response.status_code == 503

    def test_route_is_404_for_another_patients_document(self, _db_file: str) -> None:
        # Document belongs to OTHER_PID; the caller is authorized for PID and asks
        # for PID — the cross-patient guard refuses it as not found (no leak).
        document_id = _seed_sync([("allergy", "Penicillin")], patient_id=OTHER_PID)
        client = _client()
        response = _post(client, document_id=document_id, patient_id=PID)
        assert response.status_code == 404
