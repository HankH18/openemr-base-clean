"""Phase-2 delegated-token cutover — smart-mode reads/writes ride the physician.

Proves the TOKEN half of the cutover (the identity half is covered by
``test_auth_cutover_routes.py``): in ``smart`` mode an interactive read
(observations) and an interactive write (writeback) go out over the logged-in
physician's own delegated session token, so OpenEMR's native audit attributes
each action to that individual physician — NOT the shared system/password token.

The interactive clients here are the REAL per-session clients (no ``_fhir_client``
seam monkeypatch), with the outbound HTTP intercepted by ``respx`` so we can read
the ``Authorization`` bearer straight off the wire. Disabled mode is asserted to
still fall back to the system read / password-grant write path, and the poller is
confirmed to stay on its system static token, unreachable from the per-session
builders.
"""

from __future__ import annotations

import inspect
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import respx
import sqlalchemy as sa
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy.orm import Session

from copilot.auth.session import SessionCrypto, hash_session_id
from copilot.chat.service import ChatService
from copilot.config import Settings
from copilot.fhir.auth import (
    ResourceOwnerPasswordTokenProvider,
    SessionTokenProvider,
    StaticTokenProvider,
)
from copilot.memory import Base, ClinicianRow, PhysicianSessionRow
from copilot.memory.models import RoundingCursorRow
from copilot.writeback.service import WriteService, get_idempotency_store

# Fernet key shared by the env-configured app and the synchronous seeding, so a
# token encrypted at seed time decrypts back inside the running app.
_ENC_KEY = Fernet.generate_key().decode()

COOKIE = "delegated-token-cookie"
FHIR_USER = "https://fhir/Practitioner/tok"
PID = 1015
#: pid -> OpenEMR patient UUID, the shape a deployment configures.
PATIENT_UUID_TEMPLATE = "a1000000-0000-0000-0000-{pid:012d}"
FHIR_BASE = "http://oe.test/fhir"
WRITE_API = "http://oe.test/api"
PHYSICIAN_TOKEN = "physician-access-delegated-xyz"  # test fixture value, not a real secret
SYSTEM_STUB = "stub-serve-token"  # the offline/system bearer from build_token_provider


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


def _bundle(resources: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "resourceType": "Bundle",
        "type": "searchset",
        "total": len(resources),
        "entry": [{"resource": r} for r in resources],
    }


# --- fixtures ---------------------------------------------------------------


def _reset_caches() -> None:
    from copilot.config import get_settings
    from copilot.memory.db import get_engine, get_session_factory

    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()
    get_idempotency_store.cache_clear()


def _make_db(tmp_path: Any) -> str:
    db_file = tmp_path / "cutover_token.db"
    import copilot.memory.models  # noqa: F401  (registers tables on Base.metadata)

    sync_engine = sa.create_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()
    return str(db_file)


@pytest.fixture
def _smart_app(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Env-configured smart-mode app on a temp-file SQLite DB (writeback enabled)."""
    db_file = _make_db(tmp_path)
    monkeypatch.setenv("COPILOT_DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
    monkeypatch.setenv("COPILOT_AUTH_MODE", "smart")
    monkeypatch.setenv("COPILOT_PUBLIC_BASE_URL", "https://af.test")
    monkeypatch.setenv("COPILOT_SESSION_ENC_KEY", _ENC_KEY)
    monkeypatch.setenv("COPILOT_SMART_APP_CLIENT_ID", "login-client")
    monkeypatch.setenv("COPILOT_SMART_APP_CLIENT_SECRET", "shh")
    monkeypatch.setenv("COPILOT_OAUTH_TOKEN_URL", "https://openemr.test/oauth2/default/token")
    monkeypatch.setenv("COPILOT_FHIR_BASE_URL", FHIR_BASE)
    monkeypatch.setenv("COPILOT_WRITE_API_BASE_URL", WRITE_API)
    monkeypatch.setenv("COPILOT_ANTHROPIC_API_KEY", "")  # -> deterministic stub agent
    monkeypatch.setenv("COPILOT_WRITEBACK_ENABLED", "true")
    monkeypatch.setenv("COPILOT_TLS_VERIFY", "false")
    # OpenEMR's encounter routes are UUID-keyed (:puuid — see
    # apis/routes/_rest_routes_standard.inc.php:105,112), so the write client needs
    # the same pid->uuid template the read client has always used. Without it the
    # client now refuses to send (it used to send the int and 502 against real
    # OpenEMR); a deployment sets this in its env.
    monkeypatch.setenv("COPILOT_FHIR_PATIENT_ID_TEMPLATE", PATIENT_UUID_TEMPLATE)
    _reset_caches()
    yield db_file
    _reset_caches()


@pytest.fixture
def _disabled_app(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Env-configured disabled-mode app (default identity model) on a temp DB."""
    db_file = _make_db(tmp_path)
    monkeypatch.setenv("COPILOT_DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
    monkeypatch.setenv("COPILOT_FHIR_BASE_URL", FHIR_BASE)
    monkeypatch.setenv("COPILOT_ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("COPILOT_TLS_VERIFY", "false")
    _reset_caches()
    yield db_file
    _reset_caches()


# --- seeding (synchronous, into the temp DB file) ---------------------------


def _seed_session(db_file: str, cookie: str = COOKIE) -> int:
    """Seed a clinician + a live session whose access token decrypts to PHYSICIAN_TOKEN."""
    now = datetime.now(UTC)
    crypto = SessionCrypto.from_key(_ENC_KEY)
    engine = sa.create_engine(f"sqlite:///{db_file}")
    try:
        with Session(engine) as s:
            clinician = ClinicianRow(fhir_user=FHIR_USER, display_name="Dr. Token")
            s.add(clinician)
            s.flush()
            clinician_id = clinician.id
            s.add(
                PhysicianSessionRow(
                    session_id=hash_session_id(cookie),
                    clinician_id=clinician_id,
                    access_token_enc=crypto.encrypt(PHYSICIAN_TOKEN),
                    refresh_token_enc=None,
                    access_expires_at=now + timedelta(hours=1),  # fresh ⇒ no refresh call
                    scope="user/Observation.read api:oemr user/vital.crus",
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


def _client() -> TestClient:
    from copilot.api.app import create_app
    from copilot.config import get_settings

    return TestClient(create_app(get_settings(), probe_factories=[]), base_url="https://testserver")


def _authed_client(cookie: str = COOKIE) -> TestClient:
    from copilot.config import get_settings

    client = _client()
    client.cookies.set(get_settings().session_cookie_name, cookie)
    return client


# --- smart mode: the physician's delegated token goes out -------------------


class TestSmartModeDelegatedToken:
    @respx.mock
    def test_read_carries_physician_token(self, _smart_app: str) -> None:
        cid = _seed_session(_smart_app)
        _seed_cursor(_smart_app, cid, [PID])
        route = respx.get(f"{FHIR_BASE}/Observation").mock(
            return_value=Response(200, json=_bundle([_obs("o1", "Troponin I", 0.9, "ng/mL", "HH")]))
        )

        r = _authed_client().get(
            f"/v1/patients/{PID}/observations", params={"metric": "Troponin I"}
        )

        assert r.status_code == 200
        assert route.called
        auth = route.calls.last.request.headers["Authorization"]
        assert auth == f"Bearer {PHYSICIAN_TOKEN}"
        assert auth != f"Bearer {SYSTEM_STUB}"

    @respx.mock
    def test_write_carries_physician_token(self, _smart_app: str) -> None:
        cid = _seed_session(_smart_app)
        _seed_cursor(_smart_app, cid, [PID])
        today = datetime.now(UTC).date().isoformat()
        # The encounter route is keyed by the patient UUID, not the pid. This mock
        # used to echo our OWN wrong URL (/patient/1015/encounter) back to us, so it
        # stayed green while asserting a flow that was 100% broken against real
        # OpenEMR. The assertion here -- that the write carries the physician's
        # delegated token -- was always right; only this fixture was wrong.
        puuid = PATIENT_UUID_TEMPLATE.format(pid=PID)
        respx.get(f"{WRITE_API}/patient/{puuid}/encounter").mock(
            return_value=Response(200, json={"data": [{"id": "42", "date": today}]})
        )
        vital_route = respx.post(f"{WRITE_API}/patient/{PID}/encounter/42/vital").mock(
            return_value=Response(201, json={"vid": 100})
        )
        # Post-write read-back (fail-open) — mocked so respx never sees an
        # unmatched request; its bearer is the physician's too.
        respx.get(f"{FHIR_BASE}/Observation").mock(return_value=Response(200, json=_bundle([])))

        # Propose FIRST, then confirm the server-issued candidate. This test used to
        # POST straight to /confirm with a self-built candidate and no propose — which
        # was probe B of the write-binding defect (confirm accepting an unproposed
        # candidate) in smart mode. The real UI proposes before confirming; only this
        # fixture skipped it. The binding now rejects an unproposed key, so the fixture
        # is corrected to the real flow. The assertion under test — the write carries
        # the physician's delegated token, not the system stub — is unchanged.
        client = _authed_client()
        proposed = client.post(
            "/v1/writes",
            json={
                "clinician_id": cid,
                "patient_id": PID,
                "kind": "vital",
                "metric": "heart_rate",
                "raw_value": "72",
                "unit": "bpm",
            },
        )
        assert proposed.status_code == 200, proposed.text
        body = proposed.json()
        key = body["idempotency_key"]
        r = client.post(f"/v1/writes/{key}/confirm", json={"candidate": body["candidate"]})

        assert r.status_code == 200
        assert vital_route.called
        auth = vital_route.calls.last.request.headers["Authorization"]
        assert auth == f"Bearer {PHYSICIAN_TOKEN}"
        assert auth != f"Bearer {SYSTEM_STUB}"


# --- disabled mode: still the system read / password-grant write ------------


class TestDisabledModeUnchanged:
    @respx.mock
    def test_read_uses_system_stub_token(self, _disabled_app: str) -> None:
        # Disabled mode: identity from the query clinician_id; seed a cursor so the
        # authorization gate passes for that clinician.
        _seed_cursor(_disabled_app, 5, [PID])
        route = respx.get(f"{FHIR_BASE}/Observation").mock(
            return_value=Response(200, json=_bundle([_obs("o1", "Troponin I", 0.9, "ng/mL", "HH")]))
        )

        r = TestClient(_new_disabled_app()).get(
            f"/v1/patients/{PID}/observations", params={"metric": "Troponin I", "clinician_id": 5}
        )

        assert r.status_code == 200
        assert route.calls.last.request.headers["Authorization"] == f"Bearer {SYSTEM_STUB}"

    def test_system_read_provider_is_the_stub_bearer(self) -> None:
        from copilot.fhir.provider import build_token_provider

        provider = build_token_provider(Settings(fhir_base_url=FHIR_BASE))
        assert isinstance(provider, StaticTokenProvider)
        assert provider.token.access_token == SYSTEM_STUB

    def test_disabled_write_uses_password_grant_provider(self) -> None:
        from copilot.fhir.provider import build_write_token_provider

        settings = Settings(
            writeback_enabled=True,
            write_client_id="w-client",
            write_username="copilot_writer",
            write_password="pw",
            write_api_base_url=WRITE_API,
            oauth_token_url="http://oe.test/token",
        )
        provider = build_write_token_provider(settings)
        assert isinstance(provider, ResourceOwnerPasswordTokenProvider)
        assert not isinstance(provider, SessionTokenProvider)
        assert provider.username == "copilot_writer"

    def test_read_service_without_factory_falls_back_to_system(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sentinel = object()
        monkeypatch.setattr("copilot.chat.service.build_fhir_client", lambda _s: sentinel)
        assert ChatService(Settings())._fhir_client() is sentinel

    def test_write_service_without_factory_falls_back_to_password_client(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sentinel = object()
        monkeypatch.setattr("copilot.writeback.service.build_write_client", lambda _s: sentinel)
        assert WriteService(Settings())._write_client() is sentinel


# --- route helpers inject a per-session factory ONLY when a session exists ---


class TestRouteFactorySelection:
    def test_chat_reader_factory_none_without_session(self) -> None:
        from copilot.api.routes.chat import _reader_factory

        assert _reader_factory(None) is None
        assert _reader_factory("sid") is not None

    def test_rounds_reader_factory_none_without_session(self) -> None:
        from copilot.api.routes.rounds import _reader_factory

        assert _reader_factory(None) is None
        assert _reader_factory("sid") is not None

    def test_writes_factories_none_without_session(self) -> None:
        from copilot.api.routes.writes import _read_factory, _write_factory

        assert _write_factory(None) is None
        assert _read_factory(None) is None
        assert _write_factory("sid") is not None
        assert _read_factory("sid") is not None

    def test_injected_reader_factory_wins(self) -> None:
        sentinel = object()
        svc = ChatService(Settings(), fhir_client_factory=lambda: sentinel)
        assert svc._fhir_client() is sentinel


# --- poller stays on the system token, unreachable from per-session builders -


class TestPollerIsolation:
    async def test_pipeline_reader_uses_system_static_token(self) -> None:
        from copilot.worker.pipeline import RefreshPipeline

        async with RefreshPipeline(Settings(fhir_base_url=FHIR_BASE))._fhir_client() as client:
            provider = client._token_provider
            assert isinstance(provider, StaticTokenProvider)
            assert not isinstance(provider, SessionTokenProvider)
            assert provider.token.access_token == "rounds-refresh-token"

    def test_worker_modules_never_reference_session_builders(self) -> None:
        import copilot.worker.pipeline as pipeline
        import copilot.worker.runtime as runtime

        for module in (pipeline, runtime):
            src = inspect.getsource(module)
            assert "for_session" not in src
            assert "SessionTokenProvider" not in src


# --- helper ----------------------------------------------------------------


def _new_disabled_app() -> Any:
    from copilot.api.app import create_app
    from copilot.config import get_settings

    return create_app(get_settings(), probe_factories=[])
