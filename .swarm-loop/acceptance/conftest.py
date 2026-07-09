"""Shared fixtures for the frozen E2E acceptance suite (black-box over HTTP).

FROZEN GOAL HARNESS. Builds the FastAPI app via `create_app` against a temp-file
SQLite DB and a respx-faked OpenEMR, with NO Anthropic key set (so the app's
deterministic stub-agent path is exercised). Tests assert only on the HTTP
contract, so implementations stay free.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

# Make sibling `_fake_openemr` importable regardless of rootdir, and make the
# `copilot` package importable no matter how the runner was invoked (a bare
# `python run.py` puts the script dir on sys.path, not agent/).
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "agent"))
from _fake_openemr import (  # noqa: E402
    FHIR_BASE_URL,
    OAUTH_AUTHORIZE_URL,
    OAUTH_TOKEN_URL,
    build_router,
)


def _clear_caches() -> None:
    from copilot.config import get_settings
    from copilot.memory.db import get_engine, get_session_factory

    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    """Point Settings at a temp SQLite file + the fake OpenEMR; no LLM key."""
    db_file = tmp_path / "acceptance.db"
    monkeypatch.setenv("COPILOT_DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
    monkeypatch.setenv("COPILOT_FHIR_BASE_URL", FHIR_BASE_URL)
    monkeypatch.setenv("COPILOT_OAUTH_TOKEN_URL", OAUTH_TOKEN_URL)
    monkeypatch.setenv("COPILOT_OAUTH_AUTHORIZE_URL", OAUTH_AUTHORIZE_URL)
    monkeypatch.setenv("COPILOT_SMART_APP_CLIENT_ID", "test-smart")
    monkeypatch.setenv("COPILOT_BACKEND_SERVICES_CLIENT_ID", "test-backend")
    monkeypatch.setenv("COPILOT_ANTHROPIC_API_KEY", "")  # -> deterministic stub agent
    monkeypatch.setenv("COPILOT_LANGFUSE_HOST", "")
    monkeypatch.setenv("COPILOT_LANGFUSE_PUBLIC_KEY", "")
    monkeypatch.setenv("COPILOT_LANGFUSE_SECRET_KEY", "")
    _clear_caches()

    # Create schema on the same file via a loop-agnostic SYNC engine (DDL only).
    import copilot.memory.models  # noqa: F401  (registers every table on Base.metadata)
    from copilot.memory.db import Base

    sync_engine = sa.create_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()
    yield
    _clear_caches()


@pytest.fixture(autouse=True)
def fake_openemr():
    """Intercept the agent's outbound calls to OpenEMR; TestClient traffic passes through."""
    with build_router():
        yield


@pytest.fixture
def make_client():
    """Factory so a test can build a second app instance on the same DB (reload)."""

    def _make() -> TestClient:
        _clear_caches()
        from copilot.api.app import create_app
        from copilot.config import get_settings

        # probe_factories=[] -> /ready is trivially ready; chat/rounds don't need probes.
        return TestClient(create_app(get_settings(), probe_factories=[]))

    return _make


@pytest.fixture
def client(make_client) -> TestClient:
    return make_client()
