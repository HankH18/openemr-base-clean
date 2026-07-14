"""Shared fixtures for the frozen Week-2 acceptance suite (per-feature copy).

FROZEN GOAL HARNESS. Points the agent at a temp-file SQLite DB and the respx
OpenEMR fake, with NO Anthropic key (so keyless Stub collaborators are the
default), write-back enabled against the fake, and the schema pre-created from
the committed Phase-0 models. Tests assert black-box behaviour only.

Collection safety: this file imports only stdlib + pytest at module scope;
everything else happens inside fixtures, so a broken feature import can never
turn into a collection error (run.py's ran-and-failed contract).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ACC = Path(__file__).resolve().parents[1]
_AGENT = _ACC.parents[1] / "agent"
for _p in (str(_ACC), str(_AGENT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _clear_caches() -> None:
    from copilot.config import get_settings
    from copilot.memory.db import get_engine, get_session_factory

    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()
    try:
        from copilot.writeback.service import get_idempotency_store

        get_idempotency_store.cache_clear()
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    """Temp SQLite DB + fake-OpenEMR endpoints + keyless stubs, schema created."""
    import _fake_openemr as fake

    db_file = tmp_path / "acceptance.db"
    env = {
        "COPILOT_DATABASE_URL": f"sqlite+aiosqlite:///{db_file}",
        "COPILOT_FHIR_BASE_URL": fake.FHIR_BASE_URL,
        "COPILOT_OAUTH_TOKEN_URL": fake.OAUTH_TOKEN_URL,
        "COPILOT_OAUTH_AUTHORIZE_URL": fake.OAUTH_AUTHORIZE_URL,
        "COPILOT_SMART_APP_CLIENT_ID": "test-smart",
        "COPILOT_BACKEND_SERVICES_CLIENT_ID": "test-backend",
        "COPILOT_ANTHROPIC_API_KEY": "",  # -> deterministic keyless stubs
        "COPILOT_LANGFUSE_HOST": "",
        "COPILOT_LANGFUSE_PUBLIC_KEY": "",
        "COPILOT_LANGFUSE_SECRET_KEY": "",
        # Write-back enabled against the respx fake (password grant is faked).
        "COPILOT_WRITEBACK_ENABLED": "true",
        "COPILOT_WRITE_CLIENT_ID": "acc-write-client",
        "COPILOT_WRITE_CLIENT_SECRET": "acc-write-secret",
        "COPILOT_WRITE_USERNAME": "copilot_writer",
        "COPILOT_WRITE_PASSWORD": "acc-write-pass",
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    _clear_caches()

    # Create the schema via a loop-agnostic SYNC engine (DDL only) — the models
    # module registers every table (incl. the Phase-0 W2 tables) on Base.metadata.
    import copilot.memory.models  # noqa: F401
    import sqlalchemy as sa
    from copilot.memory.db import Base

    sync_engine = sa.create_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()
    yield
    _clear_caches()


@pytest.fixture
def db_path(tmp_path, _env) -> Path:
    """Path of the temp SQLite DB the agent is pointed at (for row assertions)."""
    return tmp_path / "acceptance.db"


@pytest.fixture(autouse=True)
def fake_openemr(_env):
    """Intercept the agent's outbound OpenEMR calls; yields the fake's module."""
    import _fake_openemr as fake

    fake.reset_state()
    with fake.build_router():
        yield fake
    fake.reset_state()


@pytest.fixture
def settings(_env):
    from copilot.config import get_settings

    return get_settings()


@pytest.fixture(autouse=True)
async def _engine_teardown(_env):
    """Dispose the cached async engine inside the test's own event loop."""
    yield
    try:
        from copilot.memory.db import get_engine

        await get_engine().dispose()
    except Exception:
        pass
