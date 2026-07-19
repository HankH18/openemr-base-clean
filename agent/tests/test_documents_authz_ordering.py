"""Authorization ORDERING on the document routes (Round-3 audit).

Two ordering defects on ``copilot/api/routes/documents.py``:

* **F1 (page read — PHI loaded before authz).** ``get_document_page`` loaded the
  rasterized page PNG (``get_document_pages`` — a non-deferred ``LargeBinary``)
  *before* the ``is_authorized`` check. So an existing-but-unauthorized document
  had its clinical page image read out of the DB on behalf of an off-round
  caller, and the refusal's latency then depended on whether the id existed (a
  nonexistent id 404s fast; an existing-unauthorized id 404s slower because it
  paid for the image load) — a timing oracle. The sibling ``get_document``
  authorizes *before* loading any facts; the page read must mirror it.

* **F2 (upload — feature availability leaks to an unauthenticated caller).**
  ``upload_document`` raised the ``document_ingestion_enabled`` 503 *before*
  ``resolve_acting_context`` + ``is_authorized``. The writes route runs authz
  (401/403) *before* the disabled 503 "so feature availability never leaks to an
  unauthorized caller"; the upload must match — an unauthenticated caller must be
  refused (401) without learning whether ingestion is enabled on the deployment.

Both tests are append-only and go RED on the pre-fix ordering, GREEN after.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

# --- F1: page read authorizes before loading the page image -----------------


@pytest.fixture
def _db_file(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Point Settings at a temp SQLite file and create the schema (disabled mode)."""
    from copilot.config import get_settings
    from copilot.memory.db import Base, get_engine, get_session_factory

    db_file = tmp_path / "docauthz-order.db"
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
            correlation_id="c-order",
            openemr_document_id="oe-1",
            content_hash="hash-order",
            page_count=1,
            status="extracted",
        )
        await repo.create_document_page(
            source_document_id=doc.id, page_no=1, width=10, height=10, image=b"png-bytes"
        )
        return int(doc.id)


def test_unauthorized_page_read_loads_no_page_image_at_all(
    _db_file: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F1: the refusal must precede the page-image load, not follow it.

    Loading ``get_document_pages`` (the non-deferred PNG blob) before ``is_authorized``
    meant an off-round caller had the rasterized clinical page pulled from the DB on
    their behalf, and made the 404's latency scale with whether the id existed at all
    — a timing oracle distinguishing missing (fast) from existing-unauthorized (slow).
    Authorize first, then load.
    """
    import anyio

    from copilot.memory import repository as repo_mod

    document_id = anyio.run(_seed_document, 4242)
    loaded: list[str] = []

    original = repo_mod.MemoryRepository.get_document_pages

    async def _spy(self: object, document_id: int, page_no: int | None = None) -> object:
        loaded.append("page")
        return await original(self, document_id, page_no=page_no)  # type: ignore[arg-type]

    monkeypatch.setattr(repo_mod.MemoryRepository, "get_document_pages", _spy)

    client = _client()
    # Clinician 7 never started a round on patient 4242 → off-round, forbidden.
    r = client.get(f"/v1/documents/{document_id}/pages/1", params={"clinician_id": 7})

    assert r.status_code == 404, (
        f"an off-round clinician must get an existence-hiding 404, got {r.status_code}"
    )
    assert loaded == [], (
        "an off-round caller must not cause the page image to be loaded (PHI-before-authz "
        "read + timing oracle)"
    )


# --- F2: upload authorizes before revealing the ingestion kill switch --------


@pytest.fixture
def _smart_ingestion_disabled(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Smart-mode app with the document-ingestion kill switch OFF.

    document_ingestion_enabled defaults True, so it is explicitly disabled here to
    prove the auth check wins the race against the 503.
    """
    from cryptography.fernet import Fernet

    from copilot.config import get_settings
    from copilot.memory.db import Base, get_engine, get_session_factory

    db_file = tmp_path / "upload-order.db"
    monkeypatch.setenv("COPILOT_DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
    monkeypatch.setenv("COPILOT_AUTH_MODE", "smart")
    monkeypatch.setenv("COPILOT_DOCUMENT_INGESTION_ENABLED", "false")
    monkeypatch.setenv("COPILOT_PUBLIC_BASE_URL", "https://af.test")
    monkeypatch.setenv("COPILOT_SESSION_ENC_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("COPILOT_SMART_APP_CLIENT_ID", "login-client")
    monkeypatch.setenv("COPILOT_SMART_APP_CLIENT_SECRET", "shh")
    monkeypatch.setenv(
        "COPILOT_OAUTH_AUTHORIZE_URL", "https://openemr.test/oauth2/default/authorize"
    )
    monkeypatch.setenv("COPILOT_OAUTH_TOKEN_URL", "https://openemr.test/oauth2/default/token")
    monkeypatch.setenv("COPILOT_FHIR_BASE_URL", "http://oe.test/fhir")
    monkeypatch.setenv("COPILOT_ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("COPILOT_WRITEBACK_ENABLED", "true")
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


def test_upload_authenticates_before_revealing_the_ingestion_kill_switch(
    _smart_ingestion_disabled: str,
) -> None:
    """F2: in smart mode with no session the upload is 401 — even when ingestion is off.

    Raising the ``document_ingestion_enabled`` 503 before ``resolve_acting_context``
    let an unauthenticated caller learn a deployment-level feature flag (ingestion
    on/off) they have no right to observe. The writes route runs authz (401/403)
    before its disabled 503 for exactly this reason; the upload must match: auth first
    (401), and only an authorized caller ever sees the 503.
    """
    client = _client()  # no session cookie → unauthenticated in smart mode
    r = client.post(
        "/v1/documents",
        files={"file": ("lab.pdf", b"%PDF-1.4\n", "application/pdf")},
        data={"patient_id": "4242"},
    )

    assert r.status_code == 401, (
        f"an unauthenticated caller must be refused (401) before the ingestion-disabled "
        f"503 leaks the feature flag, got {r.status_code}"
    )
