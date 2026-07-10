"""The background poller is wired into the app lifespan — but gated OFF.

These tests pin the default-off promise (the whole point of the feature
switch): with ``poller_enabled`` false the lifespan is a pure no-op and the
app boots exactly as before, touching neither Postgres nor OpenEMR. With the
switch on, the lifespan builds + starts a scheduler and stops it on shutdown,
and a tick over an empty rounding-list cohort is a deterministic no-op (no
network).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from copilot.api.app import create_app
from copilot.config import Settings
from copilot.memory import Base, MemoryRepository
from copilot.worker.runtime import build_poller_scheduler


class _SpyScheduler:
    """Records the lifespan's start/shutdown calls without a real APScheduler."""

    def __init__(self) -> None:
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True

    def shutdown(self) -> None:
        self.stopped = True


# --- default-off promise ---------------------------------------------------


def test_disabled_by_default_never_builds_a_scheduler(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default settings must not touch the poller at all."""
    builder_calls: list[Settings] = []

    def _tracking_builder(settings: Settings, observability: Any = None) -> _SpyScheduler:
        builder_calls.append(settings)
        return _SpyScheduler()

    monkeypatch.setattr("copilot.worker.runtime.build_poller_scheduler", _tracking_builder)

    settings = Settings()
    assert settings.poller_enabled is False  # the promise, in the config itself

    with TestClient(create_app(settings, probe_factories=[])) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["alive"] is True
        # /ready with no probes is ready — identical to the pre-poller app.
        ready = client.get("/ready")
        assert ready.status_code == 200
        assert ready.json()["ready"] is True

    assert builder_calls == []  # never built, never started


# --- enabled path ----------------------------------------------------------


def test_enabled_starts_and_stops_scheduler(monkeypatch: pytest.MonkeyPatch) -> None:
    """When switched on, the lifespan starts a scheduler and stops it on exit."""
    spy = _SpyScheduler()
    monkeypatch.setattr(
        "copilot.worker.runtime.build_poller_scheduler", lambda _settings, _obs=None: spy
    )

    settings = Settings(poller_enabled=True)

    with TestClient(create_app(settings, probe_factories=[])) as client:
        assert spy.started is True
        assert spy.stopped is False
        assert client.get("/health").status_code == 200  # app serves normally

    assert spy.stopped is True  # cleaned up on shutdown


def test_enabled_boots_the_real_scheduler() -> None:
    """The real wiring boots + tears down cleanly (interval keeps it idle)."""
    settings = Settings(poller_enabled=True)
    with TestClient(create_app(settings, probe_factories=[])) as client:
        assert client.get("/health").status_code == 200


# --- deterministic tick over an empty cohort -------------------------------


@pytest.fixture
def _db_file(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """A temp-file SQLite DB with the schema created (shared across sessions)."""
    from copilot.config import get_settings
    from copilot.memory.db import get_engine, get_session_factory

    db_file = tmp_path / "poller_lifespan.db"
    monkeypatch.setenv("COPILOT_DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()

    sync_engine = sa.create_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()
    yield str(db_file)
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()


@pytest.mark.asyncio
async def test_tick_over_empty_rounding_lists_is_a_noop(_db_file: str) -> None:
    """No rounding cursors ⇒ no active patients ⇒ a tick does nothing.

    This exercises the real ``active_patients`` source against a live (empty)
    DB and proves a tick is safe with no network: the poller is never invoked
    because there is nobody to poll.
    """
    from copilot.config import get_settings

    scheduler = build_poller_scheduler(get_settings())
    results = await scheduler.tick_once()
    assert results == []


@pytest.mark.asyncio
async def test_active_patients_dedupes_across_cursors(_db_file: str) -> None:
    """Distinct patient ids are gathered across every clinician's cursor."""
    from copilot.config import get_settings
    from copilot.domain.primitives import ClinicianId
    from copilot.memory.db import session_scope

    async with session_scope() as session:
        repo = MemoryRepository(session)
        await repo.upsert_rounding_cursor(ClinicianId(value=1), [10, 20, 30], 0, [])
        await repo.upsert_rounding_cursor(ClinicianId(value=2), [20, 40], 0, [])

    scheduler = build_poller_scheduler(get_settings())
    patients = list(await scheduler._active_patients())
    assert sorted(p.value for p in patients) == [10, 20, 30, 40]
