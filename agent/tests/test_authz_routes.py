"""Authorization boundary tests (UC-6) — chat refuses patients off the round.

Drives the real FastAPI app + repository against a temp-file SQLite DB. Both
FHIR readers (rounds' and chat's) are replaced with an in-memory cohort double
so no network is touched: ``POST /v1/rounds/start`` establishes the clinician's
authorized set (their rounding cursor), and ``POST /v1/chat`` is then gated
against it. A chat for a patient in the set is answered (200); a patient outside
it, or a clinician who never started a round, is refused (403).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from copilot.chat.service import ChatService
from copilot.domain.primitives import ResourceType
from copilot.rounds.service import RoundsService

CLIN = 6001
OTHER_CLIN = 6002
AUTHORIZED = 1001
ALSO_AUTHORIZED = 1002
UNAUTHORIZED = 1003


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


# Every candidate patient has a troponin so both ranking (rounds) and
# drill-down (chat) can ground against them; authorization, not data, is the gate.
_COHORT: dict[str, dict[str, list[dict[str, Any]]]] = {
    "1001": {"Observation": [_obs("obs-1001", "Troponin I", 0.9, "ng/mL", "HH")]},
    "1002": {"Observation": [_obs("obs-1002", "Troponin I", 0.7, "ng/mL", "H")]},
    "1003": {"Observation": [_obs("obs-1003", "Troponin I", 0.5, "ng/mL", None)]},
}


class _FakeFhir:
    """Async-context FHIR double: ``search`` over a cohort, ``read`` by id."""

    def __init__(self, cohort: dict[str, dict[str, list[dict[str, Any]]]]) -> None:
        self._cohort = cohort
        self._by_id: dict[tuple[str, str], dict[str, Any]] = {}
        for bytype in cohort.values():
            for rtype, rlist in bytype.items():
                for r in rlist:
                    self._by_id[(rtype, r["id"])] = r

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

    async def read(self, rtype: ResourceType, rid: str) -> dict[str, Any]:
        res = self._by_id.get((rtype.value, rid))
        if res is None:
            raise RuntimeError(f"no such resource {rtype.value}/{rid}")
        return res


# --- fixtures --------------------------------------------------------------


@pytest.fixture
def _db_file(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Point Settings at a temp SQLite file and create the schema."""
    from copilot.config import get_settings
    from copilot.memory.db import Base, get_engine, get_session_factory

    db_file = tmp_path / "authz.db"
    monkeypatch.setenv("COPILOT_DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
    monkeypatch.setenv("COPILOT_FHIR_BASE_URL", "http://oe.test/fhir")
    monkeypatch.setenv("COPILOT_ANTHROPIC_API_KEY", "")  # -> deterministic stub agent
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
    """Replace both services' FHIR readers with the in-memory cohort double."""
    monkeypatch.setattr(RoundsService, "_fhir_client", lambda self: _FakeFhir(_COHORT))
    monkeypatch.setattr(ChatService, "_fhir_client", lambda self: _FakeFhir(_COHORT))


def _client() -> TestClient:
    from copilot.api.app import create_app
    from copilot.config import get_settings
    from copilot.memory.db import get_engine, get_session_factory

    get_engine.cache_clear()
    get_session_factory.cache_clear()
    return TestClient(create_app(get_settings(), probe_factories=[]))


def _start(client: TestClient, clinician_id: int, ids: list[int]) -> Any:
    return client.post("/v1/rounds/start", json={"clinician_id": clinician_id, "patient_ids": ids})


def _chat(client: TestClient, clinician_id: int, patient_id: int) -> Any:
    return client.post(
        "/v1/chat",
        json={"clinician_id": clinician_id, "patient_id": patient_id, "message": "summarize"},
    )


# --- tests -----------------------------------------------------------------


class TestChatAuthorization:
    def test_authorized_patient_is_answered(self, _db_file: str) -> None:
        client = _client()
        assert _start(client, CLIN, [AUTHORIZED, ALSO_AUTHORIZED]).status_code == 200
        r = _chat(client, CLIN, AUTHORIZED)
        assert r.status_code == 200, f"a patient on the round must be answered, got {r.status_code}"

    def test_unauthorized_patient_is_refused(self, _db_file: str) -> None:
        client = _client()
        assert _start(client, CLIN, [AUTHORIZED, ALSO_AUTHORIZED]).status_code == 200
        r = _chat(client, CLIN, UNAUTHORIZED)
        assert r.status_code == 403, f"a patient off the round must be refused, got {r.status_code}"
        # Generic reason only — no internal detail leaked.
        assert isinstance(r.json().get("detail"), str)

    def test_clinician_without_session_is_refused(self, _db_file: str) -> None:
        client = _client()
        # OTHER_CLIN never started a round → empty authorized set.
        r = _chat(client, OTHER_CLIN, AUTHORIZED)
        assert r.status_code == 403, f"no rounding session must refuse chat, got {r.status_code}"

    def test_authorization_is_per_clinician(self, _db_file: str) -> None:
        client = _client()
        # CLIN authorizes 1001; OTHER_CLIN authorizes 1003. Neither sees the other's.
        assert _start(client, CLIN, [AUTHORIZED]).status_code == 200
        assert _start(client, OTHER_CLIN, [UNAUTHORIZED]).status_code == 200
        assert _chat(client, CLIN, UNAUTHORIZED).status_code == 403
        assert _chat(client, OTHER_CLIN, AUTHORIZED).status_code == 403
        assert _chat(client, CLIN, AUTHORIZED).status_code == 200
        assert _chat(client, OTHER_CLIN, UNAUTHORIZED).status_code == 200
