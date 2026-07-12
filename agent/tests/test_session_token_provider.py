"""SessionTokenProvider — session-backed token with refresh + rotation persist."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
import respx
from cryptography.fernet import Fernet
from httpx import Response
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from copilot.auth.service import (
    SessionScopeFactory,
    build_session_token_provider,
    make_session_token_io,
)
from copilot.auth.session import SessionCrypto, hash_session_id
from copilot.config import Settings
from copilot.fhir.auth import OAuthToken, SessionTokenProvider, TokenAcquisitionError
from copilot.memory import Base, MemoryRepository

pytestmark = pytest.mark.asyncio

_TOKEN_URL = "https://openemr.test/oauth2/default/token"


class _Store:
    """In-memory (load, save) double standing in for the DB-backed callables."""

    def __init__(self, token: OAuthToken | None) -> None:
        self.token = token
        self.saved: list[OAuthToken] = []

    async def load(self) -> OAuthToken | None:
        return self.token

    async def save(self, token: OAuthToken) -> None:
        self.saved.append(token)
        self.token = token


def _fresh(access: str, refresh: str | None = None) -> OAuthToken:
    return OAuthToken(
        access_token=access,
        token_type="Bearer",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        refresh_token=refresh,
    )


def _stale(access: str, refresh: str | None) -> OAuthToken:
    # Inside the 30s skew window ⇒ is_fresh() is False.
    return OAuthToken(
        access_token=access,
        token_type="Bearer",
        expires_at=datetime.now(UTC) + timedelta(seconds=5),
        refresh_token=refresh,
    )


class TestSessionTokenProviderUnit:
    async def test_fresh_token_returned_without_network(self) -> None:
        store = _Store(_fresh("access-fresh", "r1"))
        provider = SessionTokenProvider(
            token_url=_TOKEN_URL, client_id="c", load_token=store.load, save_token=store.save
        )
        token = await provider.get_token()
        assert token.access_token == "access-fresh"
        assert store.saved == []  # no rotation, nothing persisted

    async def test_no_session_raises(self) -> None:
        store = _Store(None)
        provider = SessionTokenProvider(
            token_url=_TOKEN_URL, client_id="c", load_token=store.load, save_token=store.save
        )
        with pytest.raises(TokenAcquisitionError):
            await provider.get_token()

    async def test_stale_without_refresh_token_raises(self) -> None:
        store = _Store(_stale("access-stale", None))
        provider = SessionTokenProvider(
            token_url=_TOKEN_URL, client_id="c", load_token=store.load, save_token=store.save
        )
        with pytest.raises(TokenAcquisitionError):
            await provider.get_token()

    @respx.mock
    async def test_stale_refreshes_and_persists_rotation(self) -> None:
        route = respx.post(_TOKEN_URL).mock(
            return_value=Response(
                200,
                json={
                    "access_token": "access-2",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                    "refresh_token": "refresh-2",
                    "scope": "user/Patient.read",
                },
            )
        )
        store = _Store(_stale("access-1", "refresh-1"))
        provider = SessionTokenProvider(
            token_url=_TOKEN_URL,
            client_id="login-client",
            client_secret="shh",
            load_token=store.load,
            save_token=store.save,
        )
        token = await provider.get_token()

        assert token.access_token == "access-2"
        assert token.refresh_token == "refresh-2"
        # The rotated token was persisted back.
        assert len(store.saved) == 1
        assert store.saved[0].access_token == "access-2"
        # Correct refresh-grant form on the wire.
        posted = dict(x.split("=", 1) for x in route.calls[0].request.content.decode().split("&"))
        assert posted["grant_type"] == "refresh_token"
        assert posted["refresh_token"] == "refresh-1"
        assert posted["client_secret"] == "shh"

    @respx.mock
    async def test_refresh_without_new_refresh_token_keeps_old(self) -> None:
        respx.post(_TOKEN_URL).mock(
            return_value=Response(
                200,
                json={"access_token": "access-2", "token_type": "Bearer", "expires_in": 3600},
            )
        )
        store = _Store(_stale("access-1", "refresh-1"))
        provider = SessionTokenProvider(
            token_url=_TOKEN_URL, client_id="c", load_token=store.load, save_token=store.save
        )
        token = await provider.get_token()
        assert token.access_token == "access-2"
        assert token.refresh_token == "refresh-1"  # carried over

    @respx.mock
    async def test_refresh_failure_raises(self) -> None:
        respx.post(_TOKEN_URL).mock(return_value=Response(400, json={"error": "invalid_grant"}))
        store = _Store(_stale("access-1", "refresh-1"))
        provider = SessionTokenProvider(
            token_url=_TOKEN_URL, client_id="c", load_token=store.load, save_token=store.save
        )
        with pytest.raises(TokenAcquisitionError):
            await provider.get_token()


# --- DB-backed end-to-end: rotation actually rewrites physician_session -------


def _settings(enc_key: str) -> Settings:
    return Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        oauth_token_url=_TOKEN_URL,
        smart_app_client_id="login-client",
        smart_app_client_secret="shh",
        session_enc_key=enc_key,
    )


@pytest_asyncio.fixture
async def scope_factory() -> AsyncIterator[SessionScopeFactory]:
    """A session_scope-compatible factory over a shared in-memory SQLite DB."""
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
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


@respx.mock
async def test_db_backed_rotation_rewrites_session_row(
    scope_factory: SessionScopeFactory,
) -> None:
    respx.post(_TOKEN_URL).mock(
        return_value=Response(
            200,
            json={
                "access_token": "rotated-access",
                "token_type": "Bearer",
                "expires_in": 3600,
                "refresh_token": "rotated-refresh",
                "scope": "user/Patient.read",
            },
        )
    )
    enc_key = Fernet.generate_key().decode()
    settings = _settings(enc_key)
    crypto = SessionCrypto.from_key(enc_key)

    session_hash = hash_session_id("cookie-xyz")
    now = datetime.now(UTC)
    async with scope_factory() as session:
        repo = MemoryRepository(session)
        clinician = await repo.create_clinician(
            fhir_user="p/rot", openemr_username=None, display_name="Dr. Rot", npi=None
        )
        await repo.create_physician_session(
            session_id=session_hash,
            clinician_id=clinician.id,
            access_token_enc=crypto.encrypt("old-access"),
            refresh_token_enc=crypto.encrypt("old-refresh"),
            access_expires_at=now + timedelta(seconds=5),  # stale ⇒ forces refresh
            scope="user/Patient.read",
            fhir_user="p/rot",
            created_at=now,
            absolute_expires_at=now + timedelta(hours=12),
        )

    provider = build_session_token_provider(
        settings, session_hash, session_scope_factory=scope_factory
    )
    token = await provider.get_token()
    assert token.access_token == "rotated-access"

    # The row now holds the ENCRYPTED rotated token — reload proves persistence.
    load, _ = make_session_token_io(settings, session_hash, session_scope_factory=scope_factory)
    reloaded = await load()
    assert reloaded is not None
    assert reloaded.access_token == "rotated-access"
    assert reloaded.refresh_token == "rotated-refresh"

    async with scope_factory() as session:
        row = await MemoryRepository(session).get_physician_session(session_hash)
        assert row is not None
        # Stored as opaque ciphertext, never plaintext.
        assert b"rotated-access" not in row.access_token_enc
        assert crypto.decrypt(row.access_token_enc) == "rotated-access"
