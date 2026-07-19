"""HIPAA access-trail coverage for the manual rounding-list refresh.

``POST /v1/rounds/refresh`` → :meth:`RefreshPipeline.refresh` reads every
patient in the clinician's rounding cursor from OpenEMR (the poller's
change-gate ``count_since`` and, on change, the resource pulls). Broad data
access in this system is accepted *only* because every access is audited, so an
unaudited live read punches a hole in exactly that compensating control.

These tests drive :meth:`RefreshPipeline.refresh` directly (no HTTP), seeding a
rounding cursor via the real repository against a temp-file SQLite DB and
injecting an in-memory FHIR double through the constructor's
``fhir_client_factory`` seam. They mirror ``test_audit.py`` (which proves the
sibling rounds/chat read paths audit) and ``test_background_routes.py`` (the
refresh pipeline's synthetic cohort). Append-only.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import datetime
from typing import Any

import pytest
import sqlalchemy as sa

from copilot.domain.primitives import ClinicianId, PatientId, ResourceType
from copilot.memory.repository import MemoryRepository
from copilot.observability.base import correlation_id_var
from copilot.worker.pipeline import RefreshPipeline

CLIN = 7001
PID = 6001


# --- synthetic FHIR double -------------------------------------------------


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


# 6001: a change (critical HH troponin) so the tick synthesizes — the fuller
# read path (count_since -> search -> resource pulls).
_COHORT: dict[str, dict[str, list[dict[str, Any]]]] = {
    "6001": {"Observation": [_obs("o-6001", "Troponin I", 0.9, "ng/mL", "HH")]},
}


def _parse(raw: str) -> datetime:
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def _last_updated(res: dict[str, Any]) -> datetime:
    return _parse(res.get("meta", {}).get("lastUpdated", "1970-01-01T00:00:00Z"))


class _FakeFhirClient:
    """Async-context FHIR double: ``count_since`` + ``search`` over the cohort."""

    def __init__(self, cohort: dict[str, dict[str, list[dict[str, Any]]]]) -> None:
        self._cohort = cohort

    async def __aenter__(self) -> _FakeFhirClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    def _pool(self, rtype: ResourceType, pid: str) -> list[dict[str, Any]]:
        return self._cohort.get(pid, {}).get(rtype.value, [])

    async def count_since(self, rtype: ResourceType, patient_id: Any, since: datetime) -> int:
        return sum(1 for r in self._pool(rtype, str(patient_id)) if _last_updated(r) > since)

    async def search(self, rtype: ResourceType, params: dict[str, str]) -> dict[str, Any]:
        pool = self._pool(rtype, params.get("patient", ""))
        raw = params.get("_lastUpdated")
        if raw and raw.startswith("gt"):
            cutoff = _parse(raw[2:])
            pool = [r for r in pool if _last_updated(r) > cutoff]
        return {
            "resourceType": "Bundle",
            "type": "searchset",
            "total": len(pool),
            "entry": [{"resource": r} for r in pool],
        }


# --- fixtures --------------------------------------------------------------


@pytest.fixture
def _db_file(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Point Settings at a temp SQLite file and create the schema."""
    from copilot.config import get_settings
    from copilot.memory.db import Base, get_engine, get_session_factory

    db_file = tmp_path / "refresh_audit.db"
    monkeypatch.setenv("COPILOT_DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
    monkeypatch.setenv("COPILOT_FHIR_BASE_URL", "http://oe.test/fhir")
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
    """Point the refresh pipeline's FHIR reader at the in-memory double.

    Same seam ``test_background_routes.py`` uses — monkeypatching ``_fhir_client``
    keeps the swap untyped, so the double need not structurally satisfy the
    ``FhirClient`` protocol.
    """
    monkeypatch.setattr(RefreshPipeline, "_fhir_client", lambda self: _FakeFhirClient(_COHORT))


def _pipeline() -> RefreshPipeline:
    from copilot.config import get_settings

    return RefreshPipeline(get_settings())


async def _seed_cursor(clinician: int, patient_ids: list[int]) -> None:
    from copilot.memory.db import session_scope

    async with session_scope() as session:
        repo = MemoryRepository(session)
        await repo.upsert_rounding_cursor(ClinicianId(value=clinician), patient_ids, 0, [])


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


# --- tests -----------------------------------------------------------------


class TestRefreshAudit:
    async def test_refresh_audits_each_patient_read(self, _db_file: str) -> None:
        await _seed_cursor(CLIN, [PID])

        token = correlation_id_var.set("corr-refresh-1")
        try:
            results = await _pipeline().refresh(ClinicianId(value=CLIN))
        finally:
            correlation_id_var.reset(token)

        assert results, "refresh should report an outcome for the seeded patient"

        refresh_rows = [r for r in _audit_rows(_db_file) if r["action"] == "rounds.refresh"]
        assert refresh_rows, "a refresh live read must invoke record_audit"
        assert any(
            r["patient_id"] == PID and r["clinician_id"] == CLIN for r in refresh_rows
        ), "the refresh audit row must name the patient and the clinician"
        assert all(
            r["correlation_id"] == "corr-refresh-1" for r in refresh_rows
        ), "the refresh audit row must carry the request correlation id"

    async def test_audit_write_failure_does_not_break_refresh(
        self, _db_file: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fail-open: a failed audit write must never 500 the refresh."""
        await _seed_cursor(CLIN, [PID])

        async def _boom(self: MemoryRepository, **_kwargs: Any) -> None:
            raise RuntimeError("audit backend down")

        monkeypatch.setattr(MemoryRepository, "record_audit", _boom)

        results = await _pipeline().refresh(ClinicianId(value=CLIN))

        assert results, "refresh must still return its per-patient outcomes"
        assert {r.patient_id.value for r in results} == {PID}
        # The audit write raised and was swallowed -> no row committed.
        assert not [r for r in _audit_rows(_db_file) if r["action"] == "rounds.refresh"]
