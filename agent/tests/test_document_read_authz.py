"""Authorization on the document READ path.

Guards a real defect found by an outside audit: ``GET /v1/documents/{id}`` and
``GET /v1/documents/{id}/pages/{n}`` took no ``Request`` and never called
``resolve_acting_context``/``is_authorized`` — only the upload (write) path was
gated. Those responses carry extracted clinical values
(``citations[].quote_or_value``) and the rendered page image of a scanned
clinical document, at a guessable integer id, on a public host.

Failure mode guarded: a clinician who has not established a round on the
document's patient (or any unauthenticated caller in smart mode) reading that
patient's PHI. Both handlers must resolve identity *before* touching the store,
so a caller cannot even probe which document ids exist.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient


@pytest.fixture
def _db_file(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Point Settings at a temp SQLite file and create the schema."""
    from copilot.config import get_settings
    from copilot.memory.db import Base, get_engine, get_session_factory

    db_file = tmp_path / "docauthz.db"
    monkeypatch.setenv("COPILOT_DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
    monkeypatch.setenv("COPILOT_FHIR_BASE_URL", "http://oe.test/fhir")
    monkeypatch.setenv("COPILOT_ANTHROPIC_API_KEY", "")
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


def _client() -> TestClient:
    from copilot.api.app import create_app
    from copilot.config import get_settings
    from copilot.memory.db import get_engine, get_session_factory

    get_engine.cache_clear()
    get_session_factory.cache_clear()
    return TestClient(create_app(get_settings(), probe_factories=[]))


async def _seed_document(patient_id: int) -> int:
    """Insert one source document (with a page image) for ``patient_id``."""
    from copilot.memory.db import session_scope
    from copilot.memory.repository import MemoryRepository

    async with session_scope() as session:
        repo = MemoryRepository(session)
        doc = await repo.create_source_document(
            patient_id=patient_id,
            doc_type="lab_pdf",
            correlation_id="c-authz",
            openemr_document_id="oe-1",
            content_hash="hash-authz",
            page_count=1,
            status="extracted",
        )
        await repo.create_document_page(
            source_document_id=doc.id, page_no=1, width=10, height=10, image=b"png-bytes"
        )
        # An extraction with a real fact. Without this, get_latest_extraction returns
        # None, get_extracted_facts is never called at all, and any test spying on the
        # fact-load path passes vacuously in BOTH arms — proving nothing.
        extraction = await repo.create_extraction(
            source_document_id=doc.id, correlation_id="c-authz", schema_version="w2-v1",
            model="stub", status="ok"
        )
        await repo.create_extracted_fact(
            extraction_id=extraction.id, field_path="hemoglobin", value="13.5"
        )
        return int(doc.id)


class TestDocumentReadAuthorization:
    """An off-round clinician must be refused both document reads."""

    def test_get_document_refuses_a_clinician_without_the_round(self, _db_file: str) -> None:
        import anyio

        document_id = anyio.run(_seed_document, 4242)
        client = _client()
        # Clinician 7 never started a round on patient 4242.
        r = client.get(f"/v1/documents/{document_id}", params={"clinician_id": 7})
        assert r.status_code == 403, (
            f"an off-round clinician must not read a document's facts/citations, got {r.status_code}"
        )

    def test_get_document_page_refuses_a_clinician_without_the_round(self, _db_file: str) -> None:
        import anyio

        document_id = anyio.run(_seed_document, 4242)
        client = _client()
        r = client.get(f"/v1/documents/{document_id}/pages/1", params={"clinician_id": 7})
        assert r.status_code == 403, (
            f"an off-round clinician must not read a scanned page image, got {r.status_code}"
        )

    def test_read_routes_declare_the_auth_dependency(self) -> None:
        # Structural guard: the regression was that these handlers took no Request
        # at all, so no auth could run. Pin the signature, not just the behavior.
        import inspect

        from copilot.api.routes.documents import get_document, get_document_page

        for handler in (get_document, get_document_page):
            params = inspect.signature(handler).parameters
            assert "request" in params, f"{handler.__name__} must take Request to resolve identity"
            assert "clinician_id" in params, f"{handler.__name__} must accept a clinician_id"


def test_an_unauthorized_read_loads_no_phi_at_all(_db_file: str, monkeypatch: object) -> None:
    """The refusal must happen before any fact is read out of the database.

    Authorizing AFTER loading the extraction and its facts meant an off-round caller
    still caused every fact for that document to be pulled into memory. Nothing was
    returned, so it was not a disclosure — but the refusal's latency then scaled with
    how many facts the document has, which is itself a signal, and it does work on
    behalf of a caller already known to be unwelcome. Fail closed, then do the work.
    """
    import anyio

    from copilot.memory import repository as repo_mod

    document_id = anyio.run(_seed_document, 4242)
    loaded: list[str] = []

    original = repo_mod.MemoryRepository.get_extracted_facts

    async def _spy(self: object, extraction_id: int) -> object:  # type: ignore[no-untyped-def]
        loaded.append("facts")
        return await original(self, extraction_id)  # type: ignore[arg-type]

    monkeypatch.setattr(repo_mod.MemoryRepository, "get_extracted_facts", _spy)  # type: ignore[attr-defined]

    client = _client()
    r = client.get(f"/v1/documents/{document_id}", params={"clinician_id": 7})

    assert r.status_code == 403
    assert loaded == [], "an off-round caller must not cause a single fact to be loaded"
