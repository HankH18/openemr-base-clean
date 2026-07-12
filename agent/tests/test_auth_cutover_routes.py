"""Phase-2 auth cutover tests — ``/v1/auth/status`` + smart-mode identity.

Proves the AUTH CUTOVER CONTRACT on the interactive data routes without touching
``disabled`` mode (that stays covered — byte-for-byte — by the existing route
tests). Everything here runs ``auth_mode="smart"`` against a temp-file SQLite DB
with a synthetically-seeded physician session (the same seeding pattern as
``test_auth_routes.py``'s ``TestCurrentClinicianSmart``), and the FHIR readers
replaced by in-memory doubles so no network is touched.

For each data route in smart mode we assert the three contract cases:

- ``401`` when there is no valid session cookie;
- ``403`` when the request carries a ``clinician_id`` that disagrees with the
  session's clinician (identity spoof — the session is authoritative);
- success resolved from the SESSION's clinician (the request omits ``clinician_id``
  entirely, yet the authorized read/round/write goes through).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from copilot.auth.session import hash_session_id
from copilot.chat.service import ChatService
from copilot.domain.primitives import ClinicianId, PatientId, ResourceType
from copilot.domain.writes import VitalWrite, WritableMetric, WriteCandidate, WriteKind
from copilot.memory import Base, ClinicianRow, PhysicianSessionRow
from copilot.memory.models import RoundingCursorRow
from copilot.rounds.service import RoundsService
from copilot.worker.pipeline import RefreshPipeline
from copilot.writeback.service import get_idempotency_store

COOKIE = "cutover-live-cookie"
FHIR_USER = "https://fhir/Practitioner/cutover"
OTHER_CLIN = 999999  # never the autoincrement id of the seeded session clinician
PID = 1015


# --- synthetic FHIR double --------------------------------------------------


def _obs(rid: str, text: str, value: float, unit: str, interp: str | None) -> dict[str, Any]:
    res: dict[str, Any] = {
        "resourceType": "Observation",
        "id": rid,
        "meta": {"lastUpdated": "2026-07-09T06:30:00Z"},
        "status": "final",
        "code": {"text": text},
        "valueQuantity": {"value": value, "unit": unit},
        "effectiveDateTime": "2026-07-09T06:30:00Z",
    }
    if interp is not None:
        res["interpretation"] = [{"coding": [{"code": interp}]}]
    return res


_COHORT: dict[str, dict[str, list[dict[str, Any]]]] = {
    str(PID): {"Observation": [_obs("o-1015", "Troponin I", 0.9, "ng/mL", "HH")]},
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


# --- fixtures ---------------------------------------------------------------


@pytest.fixture
def _smart_app(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Env-configured smart-mode app pointed at a temp-file SQLite DB."""
    from copilot.config import get_settings
    from copilot.memory.db import get_engine, get_session_factory

    db_file = tmp_path / "cutover.db"
    monkeypatch.setenv("COPILOT_DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
    monkeypatch.setenv("COPILOT_AUTH_MODE", "smart")
    monkeypatch.setenv("COPILOT_PUBLIC_BASE_URL", "https://af.test")
    monkeypatch.setenv("COPILOT_SESSION_ENC_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("COPILOT_SMART_APP_CLIENT_ID", "login-client")
    monkeypatch.setenv("COPILOT_SMART_APP_CLIENT_SECRET", "shh")
    monkeypatch.setenv(
        "COPILOT_OAUTH_AUTHORIZE_URL", "https://openemr.test/oauth2/default/authorize"
    )
    monkeypatch.setenv("COPILOT_OAUTH_TOKEN_URL", "https://openemr.test/oauth2/default/token")
    monkeypatch.setenv("COPILOT_FHIR_BASE_URL", "http://oe.test/fhir")
    monkeypatch.setenv("COPILOT_ANTHROPIC_API_KEY", "")  # -> deterministic stub agent
    monkeypatch.setenv("COPILOT_WRITEBACK_ENABLED", "true")
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()
    get_idempotency_store.cache_clear()

    import copilot.memory.models  # noqa: F401  (registers tables on Base.metadata)

    sync_engine = sa.create_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()
    yield str(db_file)
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()
    get_idempotency_store.cache_clear()


@pytest.fixture(autouse=True)
def _fake_fhir(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace every read seam with the in-memory cohort double (no network)."""
    from copilot.api.routes import observations

    monkeypatch.setattr(ChatService, "_fhir_client", lambda self: _FakeFhir(_COHORT))
    monkeypatch.setattr(RoundsService, "_fhir_client", lambda self: _FakeFhir(_COHORT))
    monkeypatch.setattr(RefreshPipeline, "_fhir_client", lambda self: _FakeFhir(_COHORT))
    monkeypatch.setattr(observations, "_fhir_client", lambda: _FakeFhir(_COHORT))


# --- seeding helpers (synchronous, into the temp DB file) -------------------


def _seed_session(db_file: str, cookie: str = COOKIE) -> int:
    """Seed a clinician + a live physician session; return the clinician id.

    Written synchronously (a plain sync engine) so the async app — running in the
    TestClient's own event loop — reads it back without a cross-loop engine.
    """
    now = datetime.now(UTC)
    engine = sa.create_engine(f"sqlite:///{db_file}")
    try:
        with Session(engine) as s:
            clinician = ClinicianRow(fhir_user=FHIR_USER, display_name="Dr. Cutover")
            s.add(clinician)
            s.flush()
            clinician_id = clinician.id
            s.add(
                PhysicianSessionRow(
                    session_id=hash_session_id(cookie),
                    clinician_id=clinician_id,
                    access_token_enc=b"opaque",
                    refresh_token_enc=None,
                    access_expires_at=now + timedelta(hours=1),
                    scope=None,
                    fhir_user=FHIR_USER,
                    created_at=now,
                    last_used_at=now,
                    absolute_expires_at=now + timedelta(hours=12),
                    revoked=False,
                )
            )
            s.commit()
        return clinician_id
    finally:
        engine.dispose()


def _seed_cursor(db_file: str, clinician_id: int, patient_ids: list[int]) -> None:
    """Seed a rounding cursor so ``is_authorized`` passes for these patients."""
    engine = sa.create_engine(f"sqlite:///{db_file}")
    try:
        with Session(engine) as s:
            s.add(
                RoundingCursorRow(
                    clinician_id=clinician_id,
                    ordered_patient_ids=patient_ids,
                    current_index=0,
                    completed_ids=[],
                )
            )
            s.commit()
    finally:
        engine.dispose()


def _audit_clinician_ids(db_file: str, action: str) -> list[int]:
    con = sqlite3.connect(db_file)
    try:
        cur = con.execute("SELECT clinician_id FROM audit_log WHERE action = ?", (action,))
        return [row[0] for row in cur.fetchall()]
    finally:
        con.close()


def _client() -> TestClient:
    from copilot.api.app import create_app
    from copilot.config import get_settings
    from copilot.memory.db import get_engine, get_session_factory

    get_engine.cache_clear()
    get_session_factory.cache_clear()
    # https base_url so the httpOnly session cookie round-trips as it would live.
    return TestClient(create_app(get_settings(), probe_factories=[]), base_url="https://testserver")


def _authed_client(cookie: str = COOKIE) -> TestClient:
    from copilot.config import get_settings

    client = _client()
    client.cookies.set(get_settings().session_cookie_name, cookie)
    return client


# --- /v1/auth/status --------------------------------------------------------


class TestAuthStatus:
    def test_disabled_reports_disabled_and_unauthenticated(self) -> None:
        # Default settings ⇒ disabled mode; the probe never errors and never auths.
        from copilot.api.app import create_app
        from copilot.config import get_settings
        from copilot.memory.db import get_engine, get_session_factory

        get_engine.cache_clear()
        get_session_factory.cache_clear()
        client = TestClient(create_app(get_settings(), probe_factories=[]))
        r = client.get("/v1/auth/status")
        assert r.status_code == 200
        assert r.json() == {"auth_mode": "disabled", "authenticated": False}

    def test_smart_without_cookie_is_unauthenticated(self, _smart_app: str) -> None:
        r = _client().get("/v1/auth/status")
        assert r.status_code == 200
        assert r.json() == {"auth_mode": "smart", "authenticated": False}

    def test_smart_with_valid_session_is_authenticated(self, _smart_app: str) -> None:
        _seed_session(_smart_app)
        r = _authed_client().get("/v1/auth/status")
        assert r.status_code == 200
        assert r.json() == {"auth_mode": "smart", "authenticated": True}

    def test_smart_with_bogus_cookie_is_unauthenticated(self, _smart_app: str) -> None:
        r = _authed_client("no-such-cookie").get("/v1/auth/status")
        assert r.status_code == 200
        assert r.json() == {"auth_mode": "smart", "authenticated": False}


# --- chat -------------------------------------------------------------------


def _chat_body(message: str, *, clinician_id: int | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"patient_id": PID, "message": message}
    if clinician_id is not None:
        body["clinician_id"] = clinician_id
    return body


class TestChatSmart:
    def test_no_session_is_401(self, _smart_app: str) -> None:
        r = _client().post("/v1/chat", json=_chat_body("summarize"))
        assert r.status_code == 401

    def test_mismatched_clinician_id_is_403(self, _smart_app: str) -> None:
        _seed_session(_smart_app)
        r = _authed_client().post("/v1/chat", json=_chat_body("summarize", clinician_id=OTHER_CLIN))
        assert r.status_code == 403

    def test_success_uses_session_clinician(self, _smart_app: str) -> None:
        cid = _seed_session(_smart_app)
        _seed_cursor(_smart_app, cid, [PID])  # authorize ONLY the session clinician
        # No clinician_id in the body — identity must come from the session cookie.
        r = _authed_client().post("/v1/chat", json=_chat_body("What is the latest troponin?"))
        assert r.status_code == 200
        # The PHI-read audit row is attributed to the SESSION's clinician, proving
        # the resolved identity — not any body-supplied id — drove the request.
        assert _audit_clinician_ids(_smart_app, "chat") == [cid]


# --- rounds -----------------------------------------------------------------


class TestRoundsSmart:
    def test_start_no_session_is_401(self, _smart_app: str) -> None:
        r = _client().post("/v1/rounds/start", json={"patient_ids": [PID]})
        assert r.status_code == 401

    def test_start_mismatched_clinician_id_is_403(self, _smart_app: str) -> None:
        _seed_session(_smart_app)
        r = _authed_client().post(
            "/v1/rounds/start", json={"clinician_id": OTHER_CLIN, "patient_ids": [PID]}
        )
        assert r.status_code == 403

    def test_start_then_current_use_session_clinician(self, _smart_app: str) -> None:
        cid = _seed_session(_smart_app)
        client = _authed_client()
        started = client.post("/v1/rounds/start", json={"patient_ids": [PID]})
        assert started.status_code == 200
        assert started.json()["current"]["patient_id"]["value"] == PID
        # current GET carries no clinician_id — the session must resolve the cursor
        # start persisted under the session's clinician.
        r = client.get("/v1/rounds/current")
        assert r.status_code == 200
        assert r.json()["current"]["patient_id"]["value"] == PID
        assert _audit_clinician_ids(_smart_app, "rounds.start") == [cid]

    def test_current_no_session_is_401(self, _smart_app: str) -> None:
        r = _client().get("/v1/rounds/current")
        assert r.status_code == 401


# --- observations -----------------------------------------------------------


class TestObservationsSmart:
    def test_no_session_is_401(self, _smart_app: str) -> None:
        r = _client().get(f"/v1/patients/{PID}/observations", params={"metric": "Troponin I"})
        assert r.status_code == 401

    def test_mismatched_clinician_id_is_403(self, _smart_app: str) -> None:
        _seed_session(_smart_app)
        r = _authed_client().get(
            f"/v1/patients/{PID}/observations",
            params={"metric": "Troponin I", "clinician_id": OTHER_CLIN},
        )
        assert r.status_code == 403

    def test_success_uses_session_clinician(self, _smart_app: str) -> None:
        cid = _seed_session(_smart_app)
        _seed_cursor(_smart_app, cid, [PID])
        # No clinician_id query param — identity from the session cookie.
        r = _authed_client().get(
            f"/v1/patients/{PID}/observations", params={"metric": "Troponin I"}
        )
        assert r.status_code == 200
        assert r.json()["patient_id"] == PID
        assert _audit_clinician_ids(_smart_app, "observations.series") == [cid]


# --- writes -----------------------------------------------------------------


def _propose_body(*, clinician_id: int | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "patient_id": PID,
        "kind": "vital",
        "raw_value": "72",
        "metric": "heart_rate",
        "unit": "bpm",
    }
    if clinician_id is not None:
        body["clinician_id"] = clinician_id
    return body


class TestWritesSmart:
    def test_propose_no_session_is_401(self, _smart_app: str) -> None:
        r = _client().post("/v1/writes", json=_propose_body())
        assert r.status_code == 401

    def test_propose_mismatched_clinician_id_is_403(self, _smart_app: str) -> None:
        _seed_session(_smart_app)
        r = _authed_client().post("/v1/writes", json=_propose_body(clinician_id=OTHER_CLIN))
        assert r.status_code == 403

    def test_propose_success_uses_session_clinician(self, _smart_app: str) -> None:
        cid = _seed_session(_smart_app)
        _seed_cursor(_smart_app, cid, [PID])
        r = _authed_client().post("/v1/writes", json=_propose_body())
        assert r.status_code == 200
        # The write-proposed audit row is attributed to the session's clinician.
        assert _audit_clinician_ids(_smart_app, "write_proposed") == [cid]

    def test_confirm_mismatched_candidate_clinician_is_403(self, _smart_app: str) -> None:
        _seed_session(_smart_app)
        candidate = WriteCandidate(
            kind=WriteKind.vital,
            patient_id=PatientId(value=PID),
            clinician_id=ClinicianId(value=OTHER_CLIN),  # not the session's clinician
            idempotency_key="k-mismatch-1",
            vital=VitalWrite(metric=WritableMetric.heart_rate, value=72, unit="bpm"),
        )
        r = _authed_client().post(
            "/v1/writes/k-mismatch-1/confirm",
            json={"candidate": candidate.model_dump(mode="json")},
        )
        assert r.status_code == 403


# --- rounds alerts + refresh (also interactive, clinician-scoped) -----------


class TestAlertsSmart:
    def test_no_session_is_401(self, _smart_app: str) -> None:
        r = _client().get("/v1/rounds/alerts")
        assert r.status_code == 401

    def test_mismatched_clinician_id_is_403(self, _smart_app: str) -> None:
        _seed_session(_smart_app)
        r = _authed_client().get("/v1/rounds/alerts", params={"clinician_id": OTHER_CLIN})
        assert r.status_code == 403

    def test_success_resolves_from_session(self, _smart_app: str) -> None:
        _seed_session(_smart_app)
        # No clinician_id query param — identity from the session cookie. No active
        # round for the session clinician ⇒ an empty (but authorized) alert list.
        r = _authed_client().get("/v1/rounds/alerts")
        assert r.status_code == 200
        assert r.json() == {"alerts": []}


class TestRefreshSmart:
    def test_no_session_is_401(self, _smart_app: str) -> None:
        r = _client().post("/v1/rounds/refresh", json={})
        assert r.status_code == 401

    def test_mismatched_clinician_id_is_403(self, _smart_app: str) -> None:
        _seed_session(_smart_app)
        r = _authed_client().post("/v1/rounds/refresh", json={"clinician_id": OTHER_CLIN})
        assert r.status_code == 403

    def test_success_resolves_from_session(self, _smart_app: str) -> None:
        _seed_session(_smart_app)
        # No clinician_id — identity from the session cookie. No active round ⇒ an
        # empty (but authorized) per-patient result list.
        r = _authed_client().post("/v1/rounds/refresh", json={})
        assert r.status_code == 200
        assert r.json() == {"results": []}
