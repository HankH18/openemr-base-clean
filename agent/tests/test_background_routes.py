"""Background refresh + deterioration-alert routes.

Drives the real FastAPI app + repository against a temp-file SQLite DB, with
both the rounds service and the refresh pipeline's FHIR reader replaced by an
in-memory double (so no network, fixed synthetic cohort). Exercises the
change-gate, verification-at-synthesis (grounded claims persist), acuity
scoring, and the not-yet-seen-critical alert rule.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from copilot.domain.primitives import ResourceType
from copilot.rounds.ranking import CRITICAL_BASE, NORMAL_SCORE
from copilot.rounds.service import RoundsService
from copilot.worker.pipeline import RefreshPipeline

CLIN = 8001


# --- synthetic FHIR helpers ------------------------------------------------


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


# 5001 critical (HH troponin), 5002 normal (no interpretation).
_COHORT: dict[str, dict[str, list[dict[str, Any]]]] = {
    "5001": {"Observation": [_obs("o-5001", "Troponin I", 0.9, "ng/mL", "HH")]},
    "5002": {"Observation": [_obs("o-5002", "Sodium", 140.0, "mmol/L", None)]},
}


def _parse(raw: str) -> datetime:
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def _last_updated(res: dict[str, Any]) -> datetime:
    return _parse(res.get("meta", {}).get("lastUpdated", "1970-01-01T00:00:00Z"))


class _FakeFhirClient:
    """Async-context FHIR double: search + count_since over the fixed cohort."""

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
    from copilot.config import get_settings
    from copilot.memory.db import Base, get_engine, get_session_factory

    db_file = tmp_path / "background.db"
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
    """Point both the rounds service and the refresh pipeline at the double."""
    monkeypatch.setattr(RoundsService, "_fhir_client", lambda self: _FakeFhirClient(_COHORT))
    monkeypatch.setattr(RefreshPipeline, "_fhir_client", lambda self: _FakeFhirClient(_COHORT))


def _client() -> TestClient:
    from copilot.api.app import create_app
    from copilot.config import get_settings
    from copilot.memory.db import get_engine, get_session_factory

    get_engine.cache_clear()
    get_session_factory.cache_clear()
    return TestClient(create_app(get_settings(), probe_factories=[]))


def _start(client: TestClient, ids: list[int]) -> Any:
    return client.post("/v1/rounds/start", json={"clinician_id": CLIN, "patient_ids": ids})


def _refresh(client: TestClient) -> Any:
    return client.post("/v1/rounds/refresh", json={"clinician_id": CLIN})


def _alerts(client: TestClient) -> Any:
    return client.get("/v1/rounds/alerts", params={"clinician_id": CLIN})


# --- tests -----------------------------------------------------------------


class TestRefresh:
    def test_reports_outcome_per_patient(self, _db_file: str) -> None:
        client = _client()
        assert _start(client, [5001, 5002]).status_code == 200
        r = _refresh(client)
        assert r.status_code == 200
        results = r.json()["results"]
        by_pid = {row["patient_id"]["value"]: row for row in results}
        assert set(by_pid) == {5001, 5002}
        assert by_pid[5001]["outcome"] == "synthesized"
        assert not by_pid[5001].get("error")

    def test_persists_grounded_claims_with_source_ref(self, _db_file: str) -> None:
        client = _client()
        _start(client, [5001])
        assert _refresh(client).status_code == 200
        card = client.get("/v1/rounds/current", params={"clinician_id": CLIN}).json()["current"]
        claims = card["summary_claims"]
        assert claims
        for claim in claims:
            assert {"resource_type", "resource_id", "field", "value"} <= set(claim["source_ref"])
        assert card["acuity_score"] >= CRITICAL_BASE

    def test_is_change_gated_idempotent(self, _db_file: str) -> None:
        client = _client()
        _start(client, [5001])
        assert _refresh(client).status_code == 200
        second = _refresh(client)
        assert second.status_code == 200
        rows = second.json()["results"]
        assert rows[0]["outcome"] == "no_change"
        for row in rows:
            assert not row.get("error")

    def test_no_active_round_returns_empty(self, _db_file: str) -> None:
        client = _client()
        r = client.post("/v1/rounds/refresh", json={"clinician_id": 9999})
        assert r.status_code == 200
        assert r.json()["results"] == []


class TestAlerts:
    def test_offered_for_unseen_critical_only(self, _db_file: str) -> None:
        client = _client()
        _start(client, [5001, 5002])
        _refresh(client)
        r = _alerts(client)
        assert r.status_code == 200
        alerted = {a["patient_id"]["value"] for a in r.json()["alerts"]}
        assert alerted == {5001}
        assert NORMAL_SCORE < CRITICAL_BASE  # sanity: 5002 stays below threshold

    def test_carries_grounded_reason(self, _db_file: str) -> None:
        client = _client()
        _start(client, [5001])
        _refresh(client)
        alerts = _alerts(client).json()["alerts"]
        assert alerts and alerts[0]["reason"].strip()

    def test_suppressed_once_patient_seen(self, _db_file: str) -> None:
        client = _client()
        _start(client, [5001, 5002])
        _refresh(client)
        # Round on 5001 -> a last_seen row exists, so it is no longer a surprise.
        client.post(
            "/v1/rounds/advance",
            json={"clinician_id": CLIN, "completed_patient_id": 5001},
        )
        alerted = {a["patient_id"]["value"] for a in _alerts(client).json()["alerts"]}
        assert 5001 not in alerted

    def test_no_active_round_returns_empty(self, _db_file: str) -> None:
        client = _client()
        r = client.get("/v1/rounds/alerts", params={"clinician_id": 9999})
        assert r.status_code == 200
        assert r.json()["alerts"] == []
