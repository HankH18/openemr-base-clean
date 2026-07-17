"""Shared fixtures for the frozen feat_rag acceptance suite (Week 2).

FROZEN GOAL HARNESS — do not edit to make a test pass. Per-test environment:
a temp-file SQLite DB, keyless provider settings (so every ``build_*`` factory
must select its Stub), cleared settings/engine caches, and the full agent
schema pre-created (the Phase-0 tables already ship in
``copilot.memory.models``).

Feature imports are deliberately NOT done here — each test imports the
``copilot.rag`` surface defensively (see ``_rag_helpers.feature_module``) so a
missing feature is a ran-and-FAILED test, never a collection or setup error.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import sqlalchemy as sa

_THIS_DIR = Path(__file__).resolve().parent
_ACC_DIR = _THIS_DIR.parent
_AGENT_DIR = _ACC_DIR.parents[1] / "agent"
for _p in (str(_THIS_DIR), str(_ACC_DIR), str(_AGENT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _clear_caches() -> None:
    from copilot.config import get_settings
    from copilot.memory.db import get_engine, get_session_factory

    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    """Temp SQLite DB + keyless settings; schema pre-created; caches cleared."""
    db_file = tmp_path / "acceptance_rag.db"
    monkeypatch.setenv("COPILOT_DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
    monkeypatch.setenv("COPILOT_FHIR_BASE_URL", "http://openemr.test/fhir")
    monkeypatch.setenv("COPILOT_OAUTH_TOKEN_URL", "http://openemr.test/oauth2/default/token")
    monkeypatch.setenv(
        "COPILOT_OAUTH_AUTHORIZE_URL", "http://openemr.test/oauth2/default/authorize"
    )
    monkeypatch.setenv("COPILOT_SMART_APP_CLIENT_ID", "test-smart")
    monkeypatch.setenv("COPILOT_BACKEND_SERVICES_CLIENT_ID", "test-backend")
    # Keyless everywhere -> every factory must return its deterministic Stub.
    monkeypatch.setenv("COPILOT_ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("COPILOT_VOYAGE_API_KEY", "")
    monkeypatch.setenv("COPILOT_COHERE_API_KEY", "")
    monkeypatch.setenv("COPILOT_LANGFUSE_HOST", "")
    monkeypatch.setenv("COPILOT_LANGFUSE_PUBLIC_KEY", "")
    monkeypatch.setenv("COPILOT_LANGFUSE_SECRET_KEY", "")
    _clear_caches()

    import copilot.memory.models  # noqa: F401  (registers every table on Base.metadata)
    from copilot.memory.db import Base

    engine = sa.create_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(engine)
    engine.dispose()
    yield
    _clear_caches()
