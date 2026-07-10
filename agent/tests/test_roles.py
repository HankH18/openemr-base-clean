"""Role-based access tests — the ClinicalRole model plus the rounds/start gate.

The enum/helper tests are pure. The route tests drive the real FastAPI app +
repository against a temp-file SQLite DB, with the FHIR reader replaced by an
in-memory double (monkeypatching ``RoundsService._fhir_client``) so no network
is touched — the same setup as ``tests/test_rounds_routes.py``.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from copilot.auth.roles import (
    ROLE_HEADER,
    ClinicalRole,
    UnknownClinicalRoleError,
    may_lead_round,
    parse_role,
)
from copilot.domain.primitives import ResourceType
from copilot.rounds.service import RoundsService

CLIN = 8802


# --- pure enum / helpers ---------------------------------------------------


class TestParseRole:
    def test_absent_header_defaults_to_physician(self) -> None:
        assert parse_role(None) is ClinicalRole.physician

    def test_empty_or_whitespace_defaults_to_physician(self) -> None:
        assert parse_role("") is ClinicalRole.physician
        assert parse_role("   ") is ClinicalRole.physician

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("physician", ClinicalRole.physician),
            ("resident", ClinicalRole.resident),
            ("attending", ClinicalRole.attending),
            ("nurse", ClinicalRole.nurse),
        ],
    )
    def test_recognized_roles_parse(self, raw: str, expected: ClinicalRole) -> None:
        assert parse_role(raw) is expected

    def test_parse_is_case_and_whitespace_insensitive(self) -> None:
        assert parse_role("  Physician ") is ClinicalRole.physician
        assert parse_role("NURSE") is ClinicalRole.nurse

    def test_unrecognized_role_raises(self) -> None:
        with pytest.raises(UnknownClinicalRoleError) as exc:
            parse_role("wizard")
        assert exc.value.raw == "wizard"


class TestMayLeadRound:
    @pytest.mark.parametrize(
        ("role", "allowed"),
        [
            (ClinicalRole.physician, True),
            (ClinicalRole.resident, True),
            (ClinicalRole.attending, True),
            (ClinicalRole.nurse, False),
        ],
    )
    def test_only_rounding_clinicians_may_lead(self, role: ClinicalRole, allowed: bool) -> None:
        assert may_lead_round(role) is allowed


# --- synthetic FHIR double (mirrors test_rounds_routes.py) -----------------


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


_COHORT: dict[str, dict[str, list[dict[str, Any]]]] = {
    "1001": {"Observation": [_obs("o-1001", "Troponin I", 0.9, "ng/mL", "HH")]},
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


@pytest.fixture
def _db_file(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Point Settings at a temp SQLite file and create the schema."""
    from copilot.config import get_settings
    from copilot.memory.db import Base, get_engine, get_session_factory

    db_file = tmp_path / "roles.db"
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


def _start(client: TestClient, role: str | None) -> Any:
    headers = {ROLE_HEADER: role} if role is not None else {}
    return client.post(
        "/v1/rounds/start",
        json={"clinician_id": CLIN, "patient_ids": [1001]},
        headers=headers,
    )


class TestRoundsStartRoleGate:
    def test_physician_may_lead(self, _db_file: str) -> None:
        assert _start(_client(), "physician").status_code == 200

    def test_resident_may_lead(self, _db_file: str) -> None:
        assert _start(_client(), "resident").status_code == 200

    def test_absent_header_defaults_to_physician_and_leads(self, _db_file: str) -> None:
        assert _start(_client(), None).status_code == 200

    def test_nurse_is_refused(self, _db_file: str) -> None:
        assert _start(_client(), "nurse").status_code == 403

    def test_unrecognized_role_is_refused(self, _db_file: str) -> None:
        assert _start(_client(), "wizard").status_code == 403
