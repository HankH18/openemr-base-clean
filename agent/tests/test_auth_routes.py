"""AuthService + /v1/auth/* route + current_clinician dependency tests.

Everything here exercises ``auth_mode="smart"`` explicitly; the disabled-mode
cases assert the endpoints stay inert (login 404, me 401). The full login
round-trip drives the real FastAPI app over an https base_url (so ``Secure``
cookies round-trip) against a temp-file SQLite DB, with only the network code
exchange stubbed — no OpenEMR and no real credentials are touched.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from urllib.parse import parse_qs, urlparse

import pytest
import pytest_asyncio
import respx
import sqlalchemy as sa
from cryptography.fernet import Fernet
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from copilot.api.deps import current_clinician
from copilot.auth.service import (
    AuthConfigError,
    AuthService,
    LoginCallbackError,
    SessionScopeFactory,
    ensure_smart_ready,
)
from copilot.auth.session import SessionCrypto, hash_session_id
from copilot.config import Settings
from copilot.domain.primitives import ClinicianId
from copilot.fhir.auth import OAuthToken
from copilot.memory import Base, ClinicianRow, MemoryRepository, PhysicianSessionRow

_NOW = datetime(2026, 7, 11, 9, 0, 0, tzinfo=UTC)


def _id_token(fhir_user: str, name: str | None = None) -> str:
    claims: dict[str, Any] = {"fhirUser": fhir_user, "sub": "s"}
    if name is not None:
        claims["name"] = name
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"e30.{payload}.sig"


def _smart_settings(enc_key: str = "unit-hmac-secret") -> Settings:
    return Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        auth_mode="smart",
        public_base_url="https://af.test",
        session_enc_key=enc_key,
        smart_app_client_id="login-client",
        smart_app_client_secret="shh",
        oauth_authorize_url="https://openemr.test/oauth2/default/authorize",
        oauth_token_url="https://openemr.test/oauth2/default/token",
        fhir_base_url="https://openemr.test/apis/default/fhir",
    )


@pytest_asyncio.fixture
async def scope_factory() -> AsyncIterator[SessionScopeFactory]:
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def factory() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    yield factory
    await engine.dispose()


# --- config guard -----------------------------------------------------------


class TestEnsureSmartReady:
    def test_disabled_is_a_noop(self) -> None:
        ensure_smart_ready(Settings(auth_mode="disabled", public_base_url="http://x"))

    def test_refuses_non_https_origin(self) -> None:
        settings = Settings(
            auth_mode="smart",
            public_base_url="http://af.test",
            session_enc_key="k",
            smart_app_client_id="c",
        )
        with pytest.raises(AuthConfigError):
            ensure_smart_ready(settings)

    def test_refuses_missing_enc_key(self) -> None:
        settings = Settings(
            auth_mode="smart", public_base_url="https://af.test", smart_app_client_id="c"
        )
        with pytest.raises(AuthConfigError):
            ensure_smart_ready(settings)


# --- begin_login ------------------------------------------------------------


@pytest.mark.asyncio
class TestBeginLogin:
    async def test_persists_txn_and_builds_authorize_url(
        self, scope_factory: SessionScopeFactory
    ) -> None:
        verifier = "V" * 64
        svc = AuthService(
            _smart_settings(),
            session_scope_factory=scope_factory,
            state_factory=lambda: "STATE-123",
            verifier_factory=lambda: verifier,
            nonce_factory=lambda: "NONCE-1",
            now_factory=lambda: _NOW,
        )
        begin = await svc.begin_login(redirect_target="/rounds")

        assert begin.state == "STATE-123"
        q = parse_qs(urlparse(begin.authorize_url).query)
        assert q["response_type"] == ["code"]
        assert q["client_id"] == ["login-client"]
        assert q["redirect_uri"] == ["https://af.test/v1/auth/callback"]
        assert q["state"] == ["STATE-123"]
        assert q["code_challenge_method"] == ["S256"]
        # aud is the PUBLIC FHIR base (public_base_url host + the fhir path),
        # not the internal read URL — OpenEMR validates aud against its
        # site_addr_oath-derived FHIR base.
        assert q["aud"] == ["https://af.test/apis/default/fhir"]
        expected_challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        assert q["code_challenge"] == [expected_challenge]

        async with scope_factory() as s:
            txn = await MemoryRepository(s).get_login_txn("STATE-123")
        assert txn is not None
        assert txn.code_verifier == verifier
        assert txn.redirect_target == "/rounds"


# --- complete_login ---------------------------------------------------------


@pytest.mark.asyncio
class TestCompleteLogin:
    @respx.mock
    async def test_exchange_creates_encrypted_session(
        self, scope_factory: SessionScopeFactory
    ) -> None:
        enc_key = Fernet.generate_key().decode()
        settings = _smart_settings(enc_key)
        async with scope_factory() as s:
            await MemoryRepository(s).create_login_txn(
                state="S1",
                code_verifier="VERIFIER-1",
                nonce="N",
                redirect_target="/census",
                created_at=_NOW,
                expires_at=_NOW + timedelta(minutes=10),
            )
        route = respx.post(settings.oauth_token_url).mock(
            return_value=Response(
                200,
                json={
                    "access_token": "access-tok",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                    "refresh_token": "refresh-tok",
                    "scope": "user/Patient.read",
                    "id_token": _id_token("https://fhir/Practitioner/uuid-x", "Dr. Xavier"),
                },
            )
        )
        svc = AuthService(settings, session_scope_factory=scope_factory, now_factory=lambda: _NOW)
        result = await svc.complete_login("CODE-1", "S1")

        assert result.redirect_target == "/census"
        assert result.clinician_id.value > 0

        # PKCE verifier + confidential secret were sent on the exchange.
        posted = dict(x.split("=", 1) for x in route.calls[0].request.content.decode().split("&"))
        assert posted["grant_type"] == "authorization_code"
        assert posted["code_verifier"] == "VERIFIER-1"
        assert posted["client_secret"] == "shh"

        # Session persisted with ENCRYPTED tokens (never plaintext).
        session_hash = hash_session_id(result.cookie_value)
        crypto = SessionCrypto.from_key(enc_key)
        async with scope_factory() as s:
            row = await MemoryRepository(s).get_physician_session(session_hash)
            # login_txn is single-use — consumed by the callback.
            assert await MemoryRepository(s).get_login_txn("S1") is None
        assert row is not None
        assert b"access-tok" not in row.access_token_enc
        assert crypto.decrypt(row.access_token_enc) == "access-tok"
        assert row.refresh_token_enc is not None
        assert crypto.decrypt(row.refresh_token_enc) == "refresh-tok"
        assert row.fhir_user == "https://fhir/Practitioner/uuid-x"

    async def test_unknown_state_raises_callback_error(
        self, scope_factory: SessionScopeFactory
    ) -> None:
        svc = AuthService(_smart_settings(), session_scope_factory=scope_factory)
        with pytest.raises(LoginCallbackError):
            await svc.complete_login("CODE", "no-such-state")

    async def test_expired_txn_raises_callback_error(
        self, scope_factory: SessionScopeFactory
    ) -> None:
        async with scope_factory() as s:
            await MemoryRepository(s).create_login_txn(
                state="OLD",
                code_verifier="V",
                nonce="N",
                redirect_target=None,
                created_at=_NOW - timedelta(hours=1),
                expires_at=_NOW - timedelta(minutes=1),
            )
        svc = AuthService(
            _smart_settings(), session_scope_factory=scope_factory, now_factory=lambda: _NOW
        )
        with pytest.raises(LoginCallbackError):
            await svc.complete_login("CODE", "OLD")


# --- resolve_session (idle + absolute TTL) ----------------------------------


@pytest.mark.asyncio
class TestResolveSession:
    async def _seed(
        self,
        scope_factory: SessionScopeFactory,
        *,
        created: datetime,
        absolute: datetime,
        cookie: str = "cookie-1",
    ) -> str:
        session_hash = hash_session_id(cookie)
        async with scope_factory() as s:
            repo = MemoryRepository(s)
            clinician = await repo.create_clinician(
                fhir_user="p/live", openemr_username="u", display_name="Dr. Live", npi=None
            )
            await repo.create_physician_session(
                session_id=session_hash,
                clinician_id=clinician.id,
                access_token_enc=b"opaque",
                refresh_token_enc=None,
                access_expires_at=created + timedelta(hours=1),
                scope=None,
                fhir_user="p/live",
                created_at=created,
                absolute_expires_at=absolute,
            )
        return cookie

    async def test_valid_session_resolves_with_identity(
        self, scope_factory: SessionScopeFactory
    ) -> None:
        cookie = await self._seed(scope_factory, created=_NOW, absolute=_NOW + timedelta(hours=12))
        svc = AuthService(
            _smart_settings(), session_scope_factory=scope_factory, now_factory=lambda: _NOW
        )
        resolved = await svc.resolve_session(cookie)
        assert resolved is not None
        assert resolved.clinician_id.value > 0
        assert resolved.display_name == "Dr. Live"
        assert resolved.fhir_user == "p/live"
        assert resolved.csrf_token  # non-empty

    async def test_idle_timeout_expires_session(self, scope_factory: SessionScopeFactory) -> None:
        cookie = await self._seed(scope_factory, created=_NOW, absolute=_NOW + timedelta(hours=12))
        # last_used_at defaults to created (_NOW); resolve past the idle window.
        later = _NOW + timedelta(seconds=1801)  # idle default 1800s
        svc = AuthService(
            _smart_settings(), session_scope_factory=scope_factory, now_factory=lambda: later
        )
        assert await svc.resolve_session(cookie) is None

    async def test_absolute_timeout_expires_session(
        self, scope_factory: SessionScopeFactory
    ) -> None:
        # Absolute cap already in the past relative to resolution time, even though
        # activity is recent.
        cookie = await self._seed(scope_factory, created=_NOW, absolute=_NOW + timedelta(minutes=1))
        later = _NOW + timedelta(minutes=2)
        svc = AuthService(
            _smart_settings(), session_scope_factory=scope_factory, now_factory=lambda: later
        )
        assert await svc.resolve_session(cookie) is None

    async def test_revoked_session_does_not_resolve(
        self, scope_factory: SessionScopeFactory
    ) -> None:
        cookie = await self._seed(scope_factory, created=_NOW, absolute=_NOW + timedelta(hours=12))
        async with scope_factory() as s:
            await MemoryRepository(s).revoke_physician_session(hash_session_id(cookie))
        svc = AuthService(
            _smart_settings(), session_scope_factory=scope_factory, now_factory=lambda: _NOW
        )
        assert await svc.resolve_session(cookie) is None


# --- routes -----------------------------------------------------------------


def _disabled_client() -> TestClient:
    from copilot.api.app import create_app
    from copilot.config import get_settings

    return TestClient(create_app(get_settings(), probe_factories=[]))


class TestRoutesDisabledMode:
    def test_login_is_404_when_disabled(self) -> None:
        assert _disabled_client().get("/v1/auth/login", follow_redirects=False).status_code == 404

    def test_me_is_401_when_no_cookie(self) -> None:
        assert _disabled_client().get("/v1/auth/me").status_code == 401


@pytest.fixture
def _smart_db(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Env-configured smart-mode app pointed at a temp-file SQLite DB."""
    from copilot.config import get_settings
    from copilot.memory.db import get_engine, get_session_factory

    db_file = tmp_path / "auth.db"
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
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()

    import copilot.memory.models  # noqa: F401

    sync_engine = sa.create_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()
    yield str(db_file)
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()


def _smart_client() -> TestClient:
    from copilot.api.app import create_app
    from copilot.config import get_settings

    # https base_url so Secure cookies round-trip through the test client.
    return TestClient(create_app(get_settings(), probe_factories=[]), base_url="https://testserver")


class TestSmartLoginRoundTrip:
    def test_login_callback_me_logout(
        self, _smart_db: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Stub only the network code-exchange; everything else is real.
        async def _fake_exchange(self: AuthService, code: str, verifier: str) -> OAuthToken:
            return OAuthToken(
                access_token="phys-access",
                token_type="Bearer",
                expires_at=datetime.now(UTC) + timedelta(hours=1),
                refresh_token="phys-refresh",
                scope="user/Patient.read",
                id_token=_id_token("https://fhir/Practitioner/round-trip", "Dr. Round"),
            )

        async def _no_revoke(self: AuthService, *tokens: str | None) -> None:
            return None  # skip the real OpenEMR revoke network call in tests

        monkeypatch.setattr(AuthService, "_exchange_code", _fake_exchange)
        monkeypatch.setattr(AuthService, "_best_effort_revoke", _no_revoke)
        client = _smart_client()

        # 1) /login → 302 to authorize; recover the state we must echo back.
        r_login = client.get("/v1/auth/login", follow_redirects=False)
        assert r_login.status_code == 302
        state = parse_qs(urlparse(r_login.headers["location"]).query)["state"][0]

        # 2) /callback → 302 to SPA + Set-Cookie (stored in the client jar).
        r_cb = client.get(f"/v1/auth/callback?code=CODE&state={state}", follow_redirects=False)
        assert r_cb.status_code == 302
        set_cookie = r_cb.headers["set-cookie"]
        assert "af_session=" in set_cookie
        assert "HttpOnly" in set_cookie and "Secure" in set_cookie

        # 3) /me → 200 with the resolved identity + a csrf token.
        r_me = client.get("/v1/auth/me")
        assert r_me.status_code == 200
        body = r_me.json()
        assert body["clinician_id"] > 0
        assert body["fhir_user"] == "https://fhir/Practitioner/round-trip"
        assert body["display_name"] == "Dr. Round"
        assert body["csrf_token"]

        # 4) /logout → 204, cookie cleared; a subsequent /me is 401.
        r_logout = client.post("/v1/auth/logout")
        assert r_logout.status_code == 204
        assert client.get("/v1/auth/me").status_code == 401

    def test_callback_bad_state_redirects_to_login_error(self, _smart_db: str) -> None:
        client = _smart_client()
        r = client.get("/v1/auth/callback?code=X&state=bogus", follow_redirects=False)
        assert r.status_code == 302
        assert "login_error=" in r.headers["location"]

    def test_callback_missing_params_redirects_to_login_error(self, _smart_db: str) -> None:
        client = _smart_client()
        r = client.get("/v1/auth/callback", follow_redirects=False)
        assert r.status_code == 302
        assert "login_error=" in r.headers["location"]


# --- current_clinician dependency -------------------------------------------


def _dep_app() -> FastAPI:
    app = FastAPI()

    @app.get("/whoami")
    async def whoami(
        clinician: Annotated[ClinicianId, Depends(current_clinician)],
    ) -> dict[str, int]:
        return {"clinician_id": clinician.value}

    return app


class TestCurrentClinicianDisabled:
    def test_falls_back_to_query_param(self) -> None:
        client = TestClient(_dep_app())
        r = client.get("/whoami", params={"clinician_id": 42})
        assert r.status_code == 200
        assert r.json() == {"clinician_id": 42}

    def test_missing_id_is_400(self) -> None:
        assert TestClient(_dep_app()).get("/whoami").status_code == 400

    def test_non_positive_id_is_400(self) -> None:
        assert TestClient(_dep_app()).get("/whoami", params={"clinician_id": 0}).status_code == 400


class TestCurrentClinicianSmart:
    def test_valid_cookie_resolves(self, _smart_db: str) -> None:
        from copilot.config import get_settings

        cookie = "live-cookie"
        session_hash = hash_session_id(cookie)
        now = datetime.now(UTC)

        # Seed synchronously into the temp DB file so the async app (running in
        # the TestClient's own event loop) reads it without a cross-loop engine.
        engine = sa.create_engine(f"sqlite:///{_smart_db}")
        with Session(engine) as s:
            clinician = ClinicianRow(fhir_user="p/dep", display_name="Dr. Dep")
            s.add(clinician)
            s.flush()
            s.add(
                PhysicianSessionRow(
                    session_id=session_hash,
                    clinician_id=clinician.id,
                    access_token_enc=b"opaque",
                    refresh_token_enc=None,
                    access_expires_at=now + timedelta(hours=1),
                    scope=None,
                    fhir_user="p/dep",
                    created_at=now,
                    last_used_at=now,
                    absolute_expires_at=now + timedelta(hours=12),
                    revoked=False,
                )
            )
            s.commit()
        engine.dispose()

        client = TestClient(_dep_app(), base_url="https://testserver")
        client.cookies.set(get_settings().session_cookie_name, cookie)
        r = client.get("/whoami")
        assert r.status_code == 200
        assert r.json()["clinician_id"] > 0

    def test_no_cookie_is_401(self, _smart_db: str) -> None:
        client = TestClient(_dep_app(), base_url="https://testserver")
        assert client.get("/whoami").status_code == 401
