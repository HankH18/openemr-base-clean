"""Observation time-series endpoint tests — grounded, fail-closed drill-down.

Drives the real FastAPI app + repository against a temp-file SQLite DB, with the
route's FHIR reader replaced by an in-memory double (monkeypatching the
module-level ``observations._fhir_client``). No network is touched; the cohort is
fixed. Authorization reuses the same rounding-cursor boundary chat enforces, so a
clinician who never opened a round is refused (403).

The double serves ``search`` over a cohort of serial troponin readings —
deliberately out of chronological order, and salted with two ungroundable
Observations (one missing its value, one missing its timestamp) to prove the
fail-closed drop rules and the oldest→newest sort.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from copilot.domain.primitives import ResourceType

CLIN = 9001
PID = 1015


# --- synthetic FHIR helpers ------------------------------------------------


def _obs(
    rid: str,
    text: str,
    *,
    value: float | None = None,
    unit: str = "ng/mL",
    effective: str | None = None,
    interp: str | None = None,
    ref_low: float | None = 0.0,
    ref_high: float | None = 0.04,
) -> dict[str, Any]:
    res: dict[str, Any] = {
        "resourceType": "Observation",
        "id": rid,
        "meta": {"lastUpdated": "2026-07-09T06:30:00Z"},
        "status": "final",
        "code": {"text": text},
    }
    if value is not None:
        res["valueQuantity"] = {"value": value, "unit": unit}
    if effective is not None:
        res["effectiveDateTime"] = effective
    if interp is not None:
        res["interpretation"] = [{"coding": [{"code": interp}]}]
    if ref_low is not None or ref_high is not None:
        res["referenceRange"] = [{"low": {"value": ref_low}, "high": {"value": ref_high}}]
    return res


# Serial troponin for 1015, intentionally shuffled — the endpoint must sort it
# oldest→newest. Two salted rows must be dropped: ``trop-noval`` has no value
# (excluded at grouping), ``trop-notime`` has a value but no timestamp.
_TROPONIN = [
    _obs("trop-6h", "Troponin I", value=0.8, effective="2026-07-09T00:00:00Z", interp="H"),
    _obs("trop-1d", "Troponin I", value=0.02, effective="2026-07-08T06:00:00Z"),
    _obs("trop-2h", "Troponin I", value=2.34, effective="2026-07-09T04:00:00Z", interp="HH"),
    _obs("trop-18h", "Troponin I", value=0.03, effective="2026-07-08T12:00:00Z"),
    _obs("trop-noval", "Troponin I", value=None, effective="2026-07-09T05:00:00Z"),
    _obs("trop-notime", "Troponin I", value=0.05, effective=None),
]

_COHORT: dict[str, dict[str, list[dict[str, Any]]]] = {
    str(PID): {"Observation": _TROPONIN},
}


class _FakeFhir:
    """Async-context FHIR double: ``search`` over the fixed cohort by patient id."""

    def __init__(self, cohort: dict[str, dict[str, list[dict[str, Any]]]]) -> None:
        self._cohort = cohort

    async def __aenter__(self) -> _FakeFhir:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def search(self, rtype: ResourceType, params: dict[str, str]) -> dict[str, Any]:
        pid = params.get("patient", "")
        resources = self._cohort.get(pid, {}).get(rtype.value, [])
        return {
            "resourceType": "Bundle",
            "type": "searchset",
            "total": len(resources),
            "entry": [{"resource": r} for r in resources],
        }


# --- fixtures --------------------------------------------------------------


@pytest.fixture
def _db_file(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Point Settings at a temp SQLite file and create the schema."""
    from copilot.config import get_settings
    from copilot.memory.db import Base, get_engine, get_session_factory

    db_file = tmp_path / "observations.db"
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
    """Replace the route's FHIR reader with the in-memory cohort double."""
    from copilot.api.routes import observations

    monkeypatch.setattr(observations, "_fhir_client", lambda: _FakeFhir(_COHORT))


@pytest.fixture(autouse=True)
def _authorize_clinician(_db_file: str) -> None:
    """Seed a rounding cursor so CLIN is authorized for PID (UC-6 boundary)."""
    import asyncio

    from copilot.domain.primitives import ClinicianId
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


def _client() -> TestClient:
    from copilot.api.app import create_app
    from copilot.config import get_settings
    from copilot.memory.db import get_engine, get_session_factory

    get_engine.cache_clear()
    get_session_factory.cache_clear()
    return TestClient(create_app(get_settings(), probe_factories=[]))


def _get(client: TestClient, *, metric: str, clinician_id: int = CLIN, patient_id: int = PID) -> Any:
    return client.get(
        f"/v1/patients/{patient_id}/observations",
        params={"metric": metric, "clinician_id": clinician_id},
    )


def _audit_rows(db_file: str) -> list[dict[str, Any]]:
    """Read the audit_log table back with a plain sync connection."""
    con = sqlite3.connect(db_file)
    try:
        cur = con.execute(
            "SELECT action, patient_id, clinician_id, correlation_id, resources_returned "
            "FROM audit_log"
        )
        cols = ("action", "patient_id", "clinician_id", "correlation_id", "resources_returned")
        return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]
    finally:
        con.close()


# --- tests -----------------------------------------------------------------


class TestObservationSeries:
    def test_authorized_returns_metric_series_sorted_oldest_to_newest(self, _db_file: str) -> None:
        client = _client()
        r = _get(client, metric="Troponin I")
        assert r.status_code == 200
        body = r.json()

        assert body["patient_id"] == PID
        assert body["metric"] == "Troponin I"
        assert body["unit"] == "ng/mL"
        assert body["reference_range"] == {"low": 0.0, "high": 0.04}

        # Oldest→newest, ungroundable rows dropped → exactly the four real points.
        assert [p["value"] for p in body["points"]] == ["0.02", "0.03", "0.8", "2.34"]
        stamps = [p["timestamp"] for p in body["points"]]
        assert stamps == sorted(stamps)
        # Verbatim ISO timestamp survives (the trailing Z is preserved).
        assert stamps[0] == "2026-07-08T06:00:00Z"
        # Abnormal flag rides along when present, '' otherwise.
        assert body["points"][-1]["abnormal"] == "HH"
        assert body["points"][0]["abnormal"] == ""
        # Every point is independently grounded to a resource id.
        assert all(p["resource_id"] for p in body["points"])

    def test_unauthorized_clinician_is_403(self, _db_file: str) -> None:
        client = _client()
        # 9999 never opened a round → no cursor → fail-closed refusal.
        r = _get(client, metric="Troponin I", clinician_id=9999)
        assert r.status_code == 403

    def test_unknown_metric_is_200_with_empty_points(self, _db_file: str) -> None:
        client = _client()
        r = _get(client, metric="Hemoglobin")
        assert r.status_code == 200
        body = r.json()
        assert body["metric"] == "Hemoglobin"
        assert body["points"] == []
        # Nothing derivable ⇒ no fabricated unit or band.
        assert body["unit"] == ""
        assert body["reference_range"] is None

    def test_points_missing_value_or_timestamp_are_dropped(self, _db_file: str) -> None:
        client = _client()
        r = _get(client, metric="Troponin I")
        assert r.status_code == 200
        points = r.json()["points"]
        ids = {p["resource_id"] for p in points}
        # The value-less and timestamp-less readings never make it into the series.
        assert "trop-noval" not in ids
        assert "trop-notime" not in ids
        assert "0.05" not in {p["value"] for p in points}
        assert len(points) == 4


class TestObservationSeriesAudit:
    def test_authorized_read_writes_an_audit_row(self, _db_file: str) -> None:
        client = _client()
        assert _get(client, metric="Troponin I").status_code == 200

        rows = [r for r in _audit_rows(_db_file) if r["action"] == "observations.series"]
        assert rows, "an observation-series PHI read must invoke record_audit"
        row = rows[0]
        assert row["patient_id"] == PID
        assert row["clinician_id"] == CLIN
        assert isinstance(row["correlation_id"], str) and row["correlation_id"]
        # resources_returned is JSON-encoded in SQLite; the returned points ride along.
        assert "trop-2h" in (row["resources_returned"] or "")

    def test_unknown_metric_still_records_the_authorized_access(self, _db_file: str) -> None:
        client = _client()
        assert _get(client, metric="Hemoglobin").status_code == 200

        rows = [r for r in _audit_rows(_db_file) if r["action"] == "observations.series"]
        assert rows, "an authorized read is audited even when the metric is absent"
        # No resources returned, but the access to the patient's chart is recorded.
        assert rows[0]["patient_id"] == PID
        assert rows[0]["resources_returned"] in ("[]", None)

    def test_403_read_writes_no_audit_row(self, _db_file: str) -> None:
        client = _client()
        assert _get(client, metric="Troponin I", clinician_id=9999).status_code == 403
        assert _audit_rows(_db_file) == [], "a refused read returns no PHI, so it is not audited"

    def test_record_audit_failure_does_not_500(
        self, _db_file: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fail-open: a broken audit write must never turn a served read into a 500."""
        from copilot.memory.repository import MemoryRepository

        async def _boom(self: Any, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError("audit write exploded")

        monkeypatch.setattr(MemoryRepository, "record_audit", _boom)

        client = _client()
        r = _get(client, metric="Troponin I")
        assert r.status_code == 200
        # The series is still produced and returned intact.
        assert [p["value"] for p in r.json()["points"]] == ["0.02", "0.03", "0.8", "2.34"]
