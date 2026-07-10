"""Rounds feature tests — deterministic ranking + the start/current/advance loop.

The ranking tests are pure (no DB, no HTTP). The route tests drive the real
FastAPI app + repository against a temp-file SQLite DB, with the FHIR reader
replaced by an in-memory double (monkeypatching ``RoundsService._fhir_client``)
so no network is touched and the synthetic cohort is fixed.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from copilot.domain.primitives import PatientId, ResourceType
from copilot.rounds.ranking import (
    CRITICAL_SCORE,
    NORMAL_SCORE,
    WARNING_SCORE,
    assess_patient,
    rank,
)
from copilot.rounds.service import RoundsService

CLIN = 7001


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


# 2001 critical (HH), 2002 warning (H), 2003 normal (no interpretation).
_COHORT: dict[str, dict[str, list[dict[str, Any]]]] = {
    "2001": {"Observation": [_obs("o-2001", "Troponin I", 0.9, "ng/mL", "HH")]},
    "2002": {"Observation": [_obs("o-2002", "Potassium", 5.6, "mmol/L", "H")]},
    "2003": {"Observation": [_obs("o-2003", "Sodium", 140.0, "mmol/L", None)]},
}


class _FakeFhirClient:
    """Async-context FHIR double returning the fixed cohort by patient id."""

    def __init__(self, cohort: dict[str, dict[str, list[dict[str, Any]]]]) -> None:
        self._cohort = cohort

    async def __aenter__(self) -> _FakeFhirClient:
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


# --- pure ranking ----------------------------------------------------------


class TestRanking:
    def test_critical_scores_above_warning_above_normal(self) -> None:
        crit = assess_patient(PatientId(value=1), _COHORT["2001"]["Observation"])
        warn = assess_patient(PatientId(value=2), _COHORT["2002"]["Observation"])
        norm = assess_patient(PatientId(value=3), _COHORT["2003"]["Observation"])
        assert crit.acuity_score == CRITICAL_SCORE
        assert warn.acuity_score == WARNING_SCORE
        assert norm.acuity_score == NORMAL_SCORE
        assert crit.acuity_score > warn.acuity_score > norm.acuity_score

    def test_rank_orders_sickest_first(self) -> None:
        norm = assess_patient(PatientId(value=3), _COHORT["2003"]["Observation"])
        crit = assess_patient(PatientId(value=1), _COHORT["2001"]["Observation"])
        warn = assess_patient(PatientId(value=2), _COHORT["2002"]["Observation"])
        ordered = [a.patient_id.value for a in rank([norm, crit, warn])]
        assert ordered == [1, 2, 3]

    def test_rank_reason_is_grounded_and_nonempty(self) -> None:
        crit = assess_patient(PatientId(value=1), _COHORT["2001"]["Observation"])
        assert crit.rank_reason.strip()
        assert "Troponin" in crit.rank_reason

    def test_ties_break_by_patient_id_ascending(self) -> None:
        a = assess_patient(PatientId(value=9), _COHORT["2003"]["Observation"])
        b = assess_patient(PatientId(value=4), _COHORT["2003"]["Observation"])
        ordered = [x.patient_id.value for x in rank([a, b])]
        assert ordered == [4, 9]

    def test_no_observations_is_normal(self) -> None:
        assessment = assess_patient(PatientId(value=1), [])
        assert assessment.acuity_score == NORMAL_SCORE
        assert assessment.rank_reason.strip()


# --- route/loop integration ------------------------------------------------


@pytest.fixture
def _db_file(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Point Settings at a temp SQLite file and create the schema."""
    from copilot.config import get_settings
    from copilot.memory.db import Base, get_engine, get_session_factory

    db_file = tmp_path / "rounds.db"
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
    """Replace the service's FHIR reader with the in-memory cohort double."""
    monkeypatch.setattr(RoundsService, "_fhir_client", lambda self: _FakeFhirClient(_COHORT))


def _client() -> TestClient:
    from copilot.api.app import create_app
    from copilot.config import get_settings
    from copilot.memory.db import get_engine, get_session_factory

    get_engine.cache_clear()
    get_session_factory.cache_clear()
    return TestClient(create_app(get_settings(), probe_factories=[]))


def _start(client: TestClient, ids: list[int]) -> Any:
    return client.post("/v1/rounds/start", json={"clinician_id": CLIN, "patient_ids": ids})


class TestRoundsLoop:
    def test_start_returns_top_card_sickest_first(self, _db_file: str) -> None:
        client = _client()
        r = _start(client, [2003, 2002, 2001])
        assert r.status_code == 200
        body = r.json()
        assert body["current"]["patient_id"]["value"] == 2001
        assert body["order"] == [2001, 2002, 2003]
        assert body["current"]["summary_claims"]
        fresh = body["current"]["freshness"]
        assert {"as_of", "age_seconds", "stale"} <= set(fresh)
        assert fresh["stale"] is False

    def test_current_matches_start(self, _db_file: str) -> None:
        client = _client()
        _start(client, [2003, 2002, 2001])
        r = client.get("/v1/rounds/current", params={"clinician_id": CLIN})
        assert r.status_code == 200
        assert r.json()["current"]["patient_id"]["value"] == 2001

    def test_current_without_session_is_404(self, _db_file: str) -> None:
        client = _client()
        r = client.get("/v1/rounds/current", params={"clinician_id": 4242})
        assert r.status_code == 404

    def test_advance_returns_next_by_acuity(self, _db_file: str) -> None:
        client = _client()
        _start(client, [2003, 2002, 2001])
        r = client.post(
            "/v1/rounds/advance",
            json={"clinician_id": CLIN, "completed_patient_id": 2001},
        )
        assert r.status_code == 200
        assert r.json()["current"]["patient_id"]["value"] == 2002

    def test_advance_to_exhaustion_reports_done(self, _db_file: str) -> None:
        client = _client()
        _start(client, [2003, 2002, 2001])
        for pid in (2001, 2002, 2003):
            r = client.post(
                "/v1/rounds/advance",
                json={"clinician_id": CLIN, "completed_patient_id": pid},
            )
            assert r.status_code == 200
        assert r.json() == {"done": True}

    def test_cursor_survives_reload(self, _db_file: str) -> None:
        client = _client()
        _start(client, [2003, 2002, 2001])
        client.post(
            "/v1/rounds/advance",
            json={"clinician_id": CLIN, "completed_patient_id": 2001},
        )
        fresh = _client()  # brand-new app on the same DB file
        r = fresh.get("/v1/rounds/current", params={"clinician_id": CLIN})
        assert r.status_code == 200
        assert r.json()["current"]["patient_id"]["value"] == 2002

    def test_jump_lands_on_target_not_the_sickest(self, _db_file: str) -> None:
        """Jump repositions to the requested patient even though 2001 ranks first."""
        client = _client()
        _start(client, [2003, 2002, 2001])  # ranks 2001 top
        r = client.post("/v1/rounds/jump", json={"clinician_id": CLIN, "patient_id": 2003})
        assert r.status_code == 200
        assert r.json()["current"]["patient_id"]["value"] == 2003
        # and the cursor persists there
        c = client.get("/v1/rounds/current", params={"clinician_id": CLIN})
        assert c.json()["current"]["patient_id"]["value"] == 2003

    def test_jump_without_session_is_404(self, _db_file: str) -> None:
        client = _client()
        r = client.post("/v1/rounds/jump", json={"clinician_id": 4242, "patient_id": 2001})
        assert r.status_code == 404

    def test_jump_to_patient_not_on_list_is_404(self, _db_file: str) -> None:
        client = _client()
        _start(client, [2003, 2002, 2001])
        r = client.post("/v1/rounds/jump", json={"clinician_id": CLIN, "patient_id": 9999})
        assert r.status_code == 404

    def test_advance_without_session_is_404(self, _db_file: str) -> None:
        client = _client()
        r = client.post(
            "/v1/rounds/advance",
            json={"clinician_id": 4242, "completed_patient_id": 1},
        )
        assert r.status_code == 404
