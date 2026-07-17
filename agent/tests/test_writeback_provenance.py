"""Write-back provenance — every derived fact links back to its source.

The spec requires the agent to "link every derived fact back to the source", and
that uploaded documents and derived observations round-trip "without creating
duplicate or **untraceable** records". Inside the agent store that already held
(a NOT NULL FK chain means no fact exists without provenance), but it was lost at
the OpenEMR write boundary: ``WriteService.propose`` took no document/fact id, the
candidate carried none, and the audit row was written with no source — so a
physician-confirmed intake-derived allergy landed in OpenEMR untraceable.

These tests pin the closed loop:

- (a) a bridge-proposed write carries the (source_document, extracted_fact) it was
  derived from, and that ``WriteSource`` reconstructs the read-side
  ``DocumentCitation``;
- (b) a physician-direct write still works with NO source — provenance is
  optional, and absent means "a human typed this", never a fabricated document;
- (c) the audit trail records the source, on both ``write_proposed`` and
  ``write_committed``, in ``source_ref`` — NOT in ``resources_returned``, which
  means "resources this action returned/created";
- (d) provenance survives the propose → physician-confirm round-trip, which is
  the only path that can reach a commit;
- (e) the agent still cannot self-commit — provenance added no path from the
  bridge to a write;
- (f) provenance reaches the OpenEMR record itself for allergies (the one
  Standard-API list route that whitelists ``comments``) and — deliberately — NOT
  for medical_problem / medication, whose payloads have no honest field for it.

Drives the real repository against a temp-file SQLite DB; no network is touched.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from copilot.domain.primitives import ClinicianId, DocumentCitation, PatientId, ResourceType, utcnow
from copilot.domain.writes import (
    AllergyWrite,
    CommittedWrite,
    IssueWriteCandidate,
    MedicalProblemWrite,
    MedicationWrite,
    WriteKind,
    WriteSource,
)
from copilot.observability import NoopObservability
from copilot.writeback.intake_bridge import IntakeWritebackBridge
from copilot.writeback.service import WriteService, get_idempotency_store

CLIN = 7301
PID = 1309


# --- doubles ---------------------------------------------------------------


class _RecordingWriter:
    """Write-client double that records the exact payload each create receives.

    Mirrors ``OpenEmrWriteClient``'s signatures (including the new ``source``
    keyword on ``create_allergy``) so a drift between the double and the real
    client shows up as a TypeError rather than a silently passing test.
    """

    def __init__(self) -> None:
        self.allergies: list[dict[str, Any]] = []
        self.problems: list[dict[str, Any]] = []
        self.meds: list[dict[str, Any]] = []

    async def __aenter__(self) -> _RecordingWriter:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def create_allergy(
        self,
        pid: PatientId,
        allergy: AllergyWrite,
        *,
        idempotency_key: str | None = None,
        source: WriteSource | None = None,
    ) -> CommittedWrite:
        self.allergies.append({"pid": pid.value, "allergy": allergy, "source": source})
        return CommittedWrite(
            resource_kind=WriteKind.allergy,
            new_id="allergy-771",
            encounter_id=None,
            committed_at=utcnow(),
        )

    async def create_medical_problem(
        self,
        pid: PatientId,
        problem: MedicalProblemWrite,
        *,
        idempotency_key: str | None = None,
    ) -> CommittedWrite:
        self.problems.append({"pid": pid.value, "problem": problem})
        return CommittedWrite(
            resource_kind=WriteKind.medical_problem,
            new_id="problem-882",
            encounter_id=None,
            committed_at=utcnow(),
        )

    async def create_medication(
        self, pid: PatientId, med: MedicationWrite, *, idempotency_key: str | None = None
    ) -> CommittedWrite:
        self.meds.append({"pid": pid.value, "med": med})
        return CommittedWrite(
            resource_kind=WriteKind.medication,
            new_id="med-993",
            encounter_id=None,
            committed_at=utcnow(),
        )


class _FakeReader:
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

    db_file = tmp_path / "provenance.db"
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


async def _seed_document(facts: list[dict[str, Any]], *, patient_id: int = PID) -> tuple[int, list[int]]:
    """Create a document + extraction + facts; return (document_id, fact_ids)."""
    from copilot.memory.db import session_scope
    from copilot.memory.repository import MemoryRepository

    async with session_scope() as session:
        repo = MemoryRepository(session)
        document = await repo.create_source_document(
            patient_id=patient_id, doc_type="intake_form", correlation_id="corr-prov-seed"
        )
        extraction = await repo.create_extraction(
            source_document_id=document.id, correlation_id="corr-prov-seed"
        )
        fact_ids: list[int] = []
        for index, fact in enumerate(facts):
            row = await repo.create_extracted_fact(
                extraction_id=extraction.id,
                field_path=f"fact[{index}]",
                value=fact["value"],
                category=fact["category"],
                page_no=fact.get("page_no"),
                bbox=fact.get("bbox"),
                match_confidence=fact.get("match_confidence"),
                supported=True,
            )
            fact_ids.append(row.id)
        return document.id, fact_ids


def _seed_sync(facts: list[dict[str, Any]], *, patient_id: int = PID) -> tuple[int, list[int]]:
    from copilot.memory.db import get_engine, get_session_factory

    result = asyncio.run(_seed_document(facts, patient_id=patient_id))
    get_engine.cache_clear()
    get_session_factory.cache_clear()
    return result


def _bridge() -> IntakeWritebackBridge:
    from copilot.config import get_settings

    return IntakeWritebackBridge(WriteService(get_settings(), NoopObservability()))


def _client() -> TestClient:
    from copilot.api.app import create_app
    from copilot.config import get_settings
    from copilot.memory.db import get_engine, get_session_factory

    get_engine.cache_clear()
    get_session_factory.cache_clear()
    return TestClient(create_app(get_settings(), probe_factories=[]))


def _audit_rows(db_file: str) -> list[dict[str, Any]]:
    """Every audit row, with the provenance column alongside resources_returned."""
    con = sqlite3.connect(db_file)
    try:
        cur = con.execute(
            "SELECT action, patient_id, clinician_id, entry_mode, resources_returned, source_ref "
            "FROM audit_log"
        )
        cols = (
            "action",
            "patient_id",
            "clinician_id",
            "entry_mode",
            "resources_returned",
            "source_ref",
        )
        return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]
    finally:
        con.close()


def _source_ref(row: dict[str, Any]) -> dict[str, Any] | None:
    """Decode the JSON-encoded source_ref column (SQLite stores it as text)."""
    raw = row["source_ref"]
    if raw is None:
        return None
    decoded: dict[str, Any] = json.loads(raw) if isinstance(raw, str) else raw
    return decoded


# --- (a) a bridge-proposed write carries its provenance ---------------------


class TestBridgeProposalCarriesProvenance:
    async def test_each_proposal_names_its_source_document_and_extracted_fact(
        self, _db_file: str
    ) -> None:
        document_id, fact_ids = await _seed_document(
            [
                {
                    "category": "allergy",
                    "value": "Penicillin",
                    "page_no": 2,
                    "bbox": [0.1, 0.2, 0.25, 0.04],
                    "match_confidence": 0.93,
                },
                {"category": "medical_problem", "value": "Hypertension", "page_no": 3},
            ]
        )

        proposals = await _bridge().propose_writes_from_document(
            document_id=document_id,
            acting_clinician=ClinicianId(value=CLIN),
            patient_id=PatientId(value=PID),
        )
        assert len(proposals) == 2
        by_kind = {p.candidate.kind: p for p in proposals}

        allergy = by_kind[WriteKind.allergy]
        source = allergy.candidate.source
        assert source is not None, "a bridge-proposed write must carry its provenance"
        # The exact two ends of the store's FK chain — not an approximation.
        assert source.source_document_id == document_id
        assert source.extracted_fact_id == fact_ids[0]
        # Enough to locate the value on the page it was read off.
        assert source.quote == "Penicillin"
        assert source.page_no == 2
        assert source.bbox == [0.1, 0.2, 0.25, 0.04]
        assert source.confidence == 0.93

        problem = by_kind[WriteKind.medical_problem]
        assert problem.candidate.source is not None
        assert problem.candidate.source.extracted_fact_id == fact_ids[1]
        # Each fact links to its OWN fact row — provenance is per-fact, not per-document.
        assert problem.candidate.source.extracted_fact_id != source.extracted_fact_id

    async def test_proposed_write_exposes_source_via_the_echo_back(self, _db_file: str) -> None:
        """``ProposedWrite.source`` delegates to the candidate — one source of truth."""
        document_id, fact_ids = await _seed_document(
            [{"category": "allergy", "value": "Sulfa drugs", "page_no": 1}]
        )
        proposals = await _bridge().propose_writes_from_document(
            document_id=document_id,
            acting_clinician=ClinicianId(value=CLIN),
            patient_id=PatientId(value=PID),
        )
        assert proposals[0].source is proposals[0].candidate.source
        assert proposals[0].source is not None
        assert proposals[0].source.extracted_fact_id == fact_ids[0]

    async def test_source_reconstructs_the_read_side_document_citation(
        self, _db_file: str
    ) -> None:
        """"Enough to reconstruct the citation" — proven, not asserted in a comment."""
        document_id, fact_ids = await _seed_document(
            [
                {
                    "category": "allergy",
                    "value": "Penicillin",
                    "page_no": 2,
                    "bbox": [0.1, 0.2, 0.25, 0.04],
                    "match_confidence": 0.93,
                }
            ]
        )
        proposals = await _bridge().propose_writes_from_document(
            document_id=document_id,
            acting_clinician=ClinicianId(value=CLIN),
            patient_id=PatientId(value=PID),
        )
        source = proposals[0].candidate.source
        assert source is not None

        citation = source.to_citation()
        assert isinstance(citation, DocumentCitation)
        assert citation.source_id == str(document_id)
        assert citation.field_or_chunk_id == str(fact_ids[0])
        assert citation.page_or_section == 2
        assert citation.quote_or_value == "Penicillin"
        assert citation.bbox == [0.1, 0.2, 0.25, 0.04]
        assert citation.confidence == 0.93

    def test_unreconciled_fact_yields_no_citation_rather_than_an_invented_page(self) -> None:
        """A fact with no page has no citation — never a fabricated page number."""
        source = WriteSource(source_document_id=5, extracted_fact_id=9, quote="Penicillin")
        assert source.page_no is None
        assert source.to_citation() is None
        # The link itself survives even when the page does not.
        assert source.source_document_id == 5
        assert source.extracted_fact_id == 9


# --- (b) the physician-direct path is unchanged -----------------------------


class TestPhysicianDirectNeedsNoSource:
    def test_direct_vital_write_has_no_source_and_still_commits(
        self, _db_file: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """entry_mode=human_direct carries no provenance — and must not need any."""
        from copilot.domain.writes import VitalWrite

        class _VitalWriter(_RecordingWriter):
            def __init__(self) -> None:
                super().__init__()
                self.vitals: list[dict[str, Any]] = []

            async def resolve_or_create_encounter(self, pid: PatientId) -> str:
                return "42"

            async def create_vital(
                self,
                pid: PatientId,
                eid: str,
                vital: VitalWrite,
                *,
                idempotency_key: str | None = None,
            ) -> CommittedWrite:
                self.vitals.append({"vital": vital})
                return CommittedWrite(
                    resource_kind=WriteKind.vital,
                    new_id="vid-555",
                    encounter_id=str(eid),
                    committed_at=utcnow(),
                )

        fake = _VitalWriter()
        monkeypatch.setattr(WriteService, "_write_client", lambda self: fake)
        monkeypatch.setattr(WriteService, "_read_client", lambda self: _FakeReader())

        client = _client()
        proposed = client.post(
            "/v1/writes",
            json={
                "clinician_id": CLIN,
                "patient_id": PID,
                "kind": "vital",
                "raw_value": "72",
                "metric": "heart_rate",
                "unit": "bpm",
            },
        )
        assert proposed.status_code == 200
        body = proposed.json()
        assert body["candidate"]["entry_mode"] == "human_direct"
        # Absent, not empty-but-present: a typed-in value has no source document.
        assert body["candidate"]["source"] is None

        confirmed = client.post(
            f"/v1/writes/{body['idempotency_key']}/confirm", json={"candidate": body["candidate"]}
        )
        assert confirmed.status_code == 200
        assert len(fake.vitals) == 1

        # The trail records the absence honestly rather than inventing a source.
        for row in _audit_rows(_db_file):
            assert row["entry_mode"] == "human_direct"
            assert _source_ref(row) is None


# --- (c) the audit trail records the source ---------------------------------


class TestAuditRecordsTheSource:
    async def test_write_proposed_row_records_the_source(self, _db_file: str) -> None:
        document_id, fact_ids = await _seed_document(
            [{"category": "allergy", "value": "Penicillin", "page_no": 2}]
        )
        await _bridge().propose_writes_from_document(
            document_id=document_id,
            acting_clinician=ClinicianId(value=CLIN),
            patient_id=PatientId(value=PID),
        )

        rows = _audit_rows(_db_file)
        assert [r["action"] for r in rows] == ["write_proposed"]
        ref = _source_ref(rows[0])
        assert ref is not None, "an agent-proposed write must name its source on the trail"
        assert ref["source_document_id"] == document_id
        assert ref["extracted_fact_id"] == fact_ids[0]
        assert ref["page_no"] == 2
        assert ref["quote"] == "Penicillin"

    async def test_source_is_recorded_in_source_ref_not_resources_returned(
        self, _db_file: str
    ) -> None:
        """Provenance must not masquerade as a resource the action returned.

        ``resources_returned`` means "the FHIR resources this action returned or
        created" — ``chat/service.py`` documents the rule and drops non-FHIR
        citations for exactly this reason. A proposal creates nothing, so it must
        still name nothing there even though it now has a source.
        """
        document_id, _ = await _seed_document(
            [{"category": "allergy", "value": "Penicillin", "page_no": 1}]
        )
        await _bridge().propose_writes_from_document(
            document_id=document_id,
            acting_clinician=ClinicianId(value=CLIN),
            patient_id=PatientId(value=PID),
        )

        row = _audit_rows(_db_file)[0]
        assert row["resources_returned"] in ("[]", None)
        assert str(document_id) not in (row["resources_returned"] or "")
        assert _source_ref(row) is not None


# --- (d) provenance survives propose → confirm ------------------------------


class TestProvenanceSurvivesConfirm:
    def test_confirmed_write_is_traceable_end_to_end(
        self, _db_file: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The whole point: a physician-confirmed derived write is traceable.

        propose (bridge) → echo-back → physician confirm → commit → audit. The
        candidate is the only thing that crosses the confirm boundary, so this is
        what proves provenance is carried by the right object.
        """
        fake = _RecordingWriter()
        monkeypatch.setattr(WriteService, "_write_client", lambda self: fake)
        monkeypatch.setattr(WriteService, "_read_client", lambda self: _FakeReader())

        document_id, fact_ids = _seed_sync(
            [{"category": "allergy", "value": "Penicillin", "page_no": 2}]
        )
        client = _client()
        proposed = client.post(
            f"/v1/writes/propose-from-document/{document_id}",
            json={"clinician_id": CLIN, "patient_id": PID},
        )
        assert proposed.status_code == 200
        proposal = proposed.json()["proposals"][0]

        # The echo-back the physician sees names the source over the wire.
        assert proposal["candidate"]["source"]["source_document_id"] == document_id
        assert proposal["candidate"]["source"]["extracted_fact_id"] == fact_ids[0]

        confirmed = client.post(
            f"/v1/writes/{proposal['idempotency_key']}/confirm",
            json={"candidate": proposal["candidate"]},
        )
        assert confirmed.status_code == 200
        assert confirmed.json()["new_id"] == "allergy-771"

        # The commit carried the source through to the write client...
        assert len(fake.allergies) == 1
        committed_source = fake.allergies[0]["source"]
        assert committed_source is not None
        assert committed_source.source_document_id == document_id
        assert committed_source.extracted_fact_id == fact_ids[0]

        # ...and the write_committed row names BOTH what it created and what it
        # was derived from. That pairing is what makes the record traceable.
        committed_rows = [r for r in _audit_rows(_db_file) if r["action"] == "write_committed"]
        assert len(committed_rows) == 1
        assert "allergy-771" in (committed_rows[0]["resources_returned"] or "")
        ref = _source_ref(committed_rows[0])
        assert ref is not None
        assert ref["source_document_id"] == document_id
        assert ref["extracted_fact_id"] == fact_ids[0]
        assert committed_rows[0]["entry_mode"] == "agent_proposed_physician_confirmed"


# --- (e) the agent still cannot commit --------------------------------------


class TestAgentStillCannotCommit:
    async def test_bridge_never_builds_a_write_client_even_with_provenance(
        self, _db_file: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Threading provenance added no path from the bridge to a commit.

        The write/read clients are patched to explode if built; the propose path
        never builds one, so the bridge completing at all is a structural proof
        that no OpenEMR write occurred.
        """

        def _boom(self: WriteService) -> Any:
            raise AssertionError("the bridge propose path must never build a write client")

        monkeypatch.setattr(WriteService, "_write_client", _boom)
        monkeypatch.setattr(WriteService, "_read_client", _boom)

        document_id, _ = await _seed_document(
            [
                {"category": "allergy", "value": "Penicillin", "page_no": 1},
                {"category": "medication", "value": "Lisinopril 10mg", "page_no": 1},
                {"category": "medical_problem", "value": "Hypertension", "page_no": 1},
            ]
        )
        proposals = await _bridge().propose_writes_from_document(
            document_id=document_id,
            acting_clinician=ClinicianId(value=CLIN),
            patient_id=PatientId(value=PID),
        )
        assert len(proposals) == 3
        assert all(p.candidate.source is not None for p in proposals)

        actions = [r["action"] for r in _audit_rows(_db_file)]
        assert actions.count("write_proposed") == 3
        assert "write_committed" not in actions
        assert "write_failed" not in actions

    def test_bridge_exposes_no_commit_path(self) -> None:
        """The bridge's public surface offers nothing that could commit."""
        public = {name for name in dir(IntakeWritebackBridge) if not name.startswith("_")}
        assert public == {"propose_writes_from_document"}
        assert not hasattr(IntakeWritebackBridge, "commit")


# --- (f) provenance reaches OpenEMR only where there is an honest field -----


class TestProvenanceInTheOpenEmrRecord:
    """Only the allergy route has a real place for provenance — verified upstream.

    ``AllergyIntoleranceRestController::WHITELISTED_FIELDS`` includes ``comments``
    and ``lists.comments`` is a real column, so an allergy record can carry its
    own source. ``ConditionRestController``'s whitelist excludes ``comments``
    (``filterData`` silently drops it) and ``ListService::insert`` binds a fixed
    column list with no ``comments`` — so for medical_problem / medication there
    is no honest field, and we send nothing rather than stuffing provenance into
    a clinical field like ``title`` or ``diagnosis``.
    """

    def test_allergy_payload_carries_the_provenance_comment(self) -> None:
        import httpx

        from copilot.fhir.write_client import OpenEmrWriteClient

        seen: dict[str, Any] = {}

        def _handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["body"] = json.loads(request.content)
            return httpx.Response(201, json={"id": 4242})

        class _Token:
            token_type = "Bearer"
            access_token = "t"

        class _Provider:
            async def get_token(self, force: bool = False) -> Any:
                return _Token()

        async def _run() -> None:
            transport = httpx.MockTransport(_handler)
            async with httpx.AsyncClient(transport=transport) as http:
                client = OpenEmrWriteClient("http://oe.test/api", _Provider(), http_client=http)
                await client.create_allergy(
                    PatientId(value=PID),
                    AllergyWrite(title="Penicillin", begdate="2026-07-16"),
                    source=WriteSource(
                        source_document_id=12,
                        extracted_fact_id=345,
                        quote="Penicillin",
                        page_no=2,
                    ),
                )

        asyncio.run(_run())

        comments = seen["body"]["comments"]
        # The comment names both ends of the chain, so the chart row alone is
        # traceable back to the scanned page.
        assert "12" in comments
        assert "345" in comments
        assert "page 2" in comments
        # It is attribution, not a clinical assertion.
        assert "physician-confirmed" in comments
        # The clinical fields stay clean — provenance never leaks into them.
        assert seen["body"]["title"] == "Penicillin"

    def test_allergy_without_a_source_sends_no_comments_field(self) -> None:
        """No source ⇒ no comment. We never write an empty or invented provenance."""
        import httpx

        from copilot.fhir.write_client import OpenEmrWriteClient

        seen: dict[str, Any] = {}

        def _handler(request: httpx.Request) -> httpx.Response:
            seen["body"] = json.loads(request.content)
            return httpx.Response(201, json={"id": 4242})

        class _Token:
            token_type = "Bearer"
            access_token = "t"

        class _Provider:
            async def get_token(self, force: bool = False) -> Any:
                return _Token()

        async def _run() -> None:
            transport = httpx.MockTransport(_handler)
            async with httpx.AsyncClient(transport=transport) as http:
                client = OpenEmrWriteClient("http://oe.test/api", _Provider(), http_client=http)
                await client.create_allergy(
                    PatientId(value=PID), AllergyWrite(title="Penicillin", begdate="2026-07-16")
                )

        asyncio.run(_run())
        assert "comments" not in seen["body"]

    def test_medical_problem_and_medication_payloads_carry_no_provenance(self) -> None:
        """No honest field upstream ⇒ we send none, and never smuggle it elsewhere.

        Guards the "do not stuff data where it doesn't belong" rule: if someone
        later adds provenance to these payloads without first fixing the upstream
        whitelist / INSERT, it would be silently dropped by OpenEMR — a record
        that looks traceable in our code and is not in the chart.
        """
        import httpx

        from copilot.fhir.write_client import OpenEmrWriteClient

        bodies: list[dict[str, Any]] = []

        def _handler(request: httpx.Request) -> httpx.Response:
            bodies.append(json.loads(request.content))
            return httpx.Response(201, json={"id": 7})

        class _Token:
            token_type = "Bearer"
            access_token = "t"

        class _Provider:
            async def get_token(self, force: bool = False) -> Any:
                return _Token()

        async def _run() -> None:
            transport = httpx.MockTransport(_handler)
            async with httpx.AsyncClient(transport=transport) as http:
                client = OpenEmrWriteClient("http://oe.test/api", _Provider(), http_client=http)
                await client.create_medical_problem(
                    PatientId(value=PID),
                    MedicalProblemWrite(title="Hypertension", begdate="2026-07-16"),
                )
                await client.create_medication(
                    PatientId(value=PID),
                    MedicationWrite(title="Lisinopril 10mg", begdate="2026-07-16"),
                )

        asyncio.run(_run())

        for body in bodies:
            assert "comments" not in body
            # Nothing smuggled into the clinical fields either.
            assert "AgentForge" not in json.dumps(body)
            assert "extracted_fact" not in json.dumps(body)

    def test_issue_candidate_source_is_optional_on_the_type(self) -> None:
        """The agent-proposed type still parses with no source (backward-compatible)."""
        candidate = IssueWriteCandidate(
            kind=WriteKind.allergy,
            patient_id=PatientId(value=PID),
            clinician_id=ClinicianId(value=CLIN),
            idempotency_key="k-1",
            allergy=AllergyWrite(title="Penicillin", begdate="2026-07-16"),
        )
        assert candidate.source is None
