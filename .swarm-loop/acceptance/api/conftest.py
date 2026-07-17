"""Shared fixtures for the frozen feat_api acceptance suite (Week 2, F8).

FROZEN GOAL HARNESS — do not edit to make a test pass. Mirrors the Week-1
acceptance conftest: the FastAPI app is built via ``create_app`` against a
temp-file SQLite DB and a respx-faked OpenEMR (``_fake_openemr``), with NO
LLM/embedder/reranker keys set so the app's deterministic stub paths are
exercised. Tests are black-box over HTTP; packaging criteria are deterministic
file checks. No network, no live API, no LLM.

Contract notes pinned by this suite (implementers build against these):
- ``auth_mode=disabled`` (the acceptance auth mode): the acting clinician is
  identified in-band — JSON ``clinician_id`` on chat/rounds, a ``clinician_id``
  form field on the multipart document upload. The RBAC rounding-list gate is
  the set of patients established via ``POST /v1/rounds/start``.
- Missing Week-2 surface must register as a FAILED test (ran-and-failed),
  never a collection error: nothing Week-2-only is imported at module scope.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

_HERE = Path(__file__).resolve()
ACCEPTANCE_DIR = _HERE.parents[1]  # .swarm-loop/acceptance/
REPO_ROOT = _HERE.parents[3]
AGENT_DIR = REPO_ROOT / "agent"

# Make the sibling `_fake_openemr` harness module and the `copilot` package
# importable no matter how the runner was invoked.
for _p in (str(ACCEPTANCE_DIR), str(AGENT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from _fake_openemr import (  # noqa: E402
    FHIR_BASE_URL,
    OAUTH_AUTHORIZE_URL,
    OAUTH_TOKEN_URL,
    build_router,
    reset_state,
)

CLINICIAN_ID = 9001


def _clear_caches() -> None:
    from copilot.config import get_settings
    from copilot.memory.db import get_engine, get_session_factory

    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()


def _minimal_pdf(text: str = "Hemoglobin 13.5 g/dL") -> bytes:
    """A deterministic, valid, single-page PDF (byte-stable; no dependency)."""
    stream = f"BT /F1 18 Tf 72 720 Td ({text}) Tj ET".encode("ascii")
    objs: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"
        ),
        b"<< /Length "
        + str(len(stream)).encode("ascii")
        + b" >>\nstream\n"
        + stream
        + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode("ascii") + body + b"\nendobj\n"
    xref_pos = len(out)
    out += f"xref\n0 {len(objs) + 1}\n".encode("ascii")
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode("ascii")
    out += (
        f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_pos}\n%%EOF\n"
    ).encode("ascii")
    return bytes(out)


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    """Point Settings at a temp SQLite file + the fake OpenEMR; no API keys."""
    db_file = tmp_path / "acceptance.db"
    monkeypatch.setenv("COPILOT_DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
    monkeypatch.setenv("COPILOT_FHIR_BASE_URL", FHIR_BASE_URL)
    monkeypatch.setenv("COPILOT_OAUTH_TOKEN_URL", OAUTH_TOKEN_URL)
    monkeypatch.setenv("COPILOT_OAUTH_AUTHORIZE_URL", OAUTH_AUTHORIZE_URL)
    monkeypatch.setenv("COPILOT_SMART_APP_CLIENT_ID", "test-smart")
    monkeypatch.setenv("COPILOT_BACKEND_SERVICES_CLIENT_ID", "test-backend")
    monkeypatch.setenv("COPILOT_AUTH_MODE", "disabled")
    monkeypatch.setenv("COPILOT_ANTHROPIC_API_KEY", "")  # -> deterministic stub agent
    monkeypatch.setenv("COPILOT_VOYAGE_API_KEY", "")  # -> stub embedder (Week 2)
    monkeypatch.setenv("COPILOT_COHERE_API_KEY", "")  # -> stub reranker (Week 2)
    monkeypatch.setenv("COPILOT_LANGFUSE_HOST", "")
    monkeypatch.setenv("COPILOT_LANGFUSE_PUBLIC_KEY", "")
    monkeypatch.setenv("COPILOT_LANGFUSE_SECRET_KEY", "")
    _clear_caches()

    # Create schema on the same file via a loop-agnostic SYNC engine (DDL only).
    # Whatever Week-2 tables exist on Base.metadata at run time are included.
    import copilot.memory.models  # noqa: F401  (registers every table on Base.metadata)
    from copilot.memory.db import Base

    sync_engine = sa.create_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()
    yield
    _clear_caches()


@pytest.fixture(autouse=True)
def fake_openemr():
    """Intercept outbound OpenEMR calls; in-process TestClient traffic passes."""
    reset_state()
    with build_router():
        yield


@pytest.fixture
def make_client():
    """App factory. Default: trivially-ready probes (``[]``).

    Pass ``probe_factories=None`` to get the app's REAL probe wiring (the
    graded-/ready criterion needs it).
    """

    def _make(probe_factories: list | None = ...) -> TestClient:  # type: ignore[valid-type]
        _clear_caches()
        from copilot.api.app import create_app
        from copilot.config import get_settings

        factories = [] if probe_factories is ... else probe_factories
        return TestClient(create_app(get_settings(), probe_factories=factories))

    return _make


@pytest.fixture
def client(make_client) -> TestClient:
    return make_client()


@pytest.fixture
def start_rounds():
    """Establish the clinician's rounding list (the RBAC authorization set)."""

    def _start(client: TestClient, patient_ids, clinician_id: int = CLINICIAN_ID):
        r = client.post(
            "/v1/rounds/start",
            json={"clinician_id": clinician_id, "patient_ids": list(patient_ids)},
        )
        assert r.status_code == 200, (
            f"harness precondition: POST /v1/rounds/start -> {r.status_code}: {r.text[:200]}"
        )
        return r

    return _start


@pytest.fixture
def pdf_bytes() -> bytes:
    return _minimal_pdf()


@pytest.fixture
def upload_document(pdf_bytes):
    """Multipart POST /v1/documents (file, patient_id, doc_type, clinician_id)."""

    def _upload(
        client: TestClient,
        patient_id: int,
        *,
        clinician_id: int = CLINICIAN_ID,
        doc_type: str = "lab_pdf",
        content: bytes | None = None,
        filename: str = "lab.pdf",
    ):
        return client.post(
            "/v1/documents",
            files={"file": (filename, content or pdf_bytes, "application/pdf")},
            data={
                "patient_id": str(patient_id),
                "doc_type": doc_type,
                "clinician_id": str(clinician_id),
            },
        )

    return _upload
