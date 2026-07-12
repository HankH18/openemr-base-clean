"""AuthService — orchestrates the per-physician SMART ``authorization_code`` flow.

The single entry point the ``/v1/auth/*`` routes call. It ties together the
pieces (crypto/cookies in ``session``, identity mapping in ``identity``, the
token providers in ``fhir.auth``, and the ``physician_session``/``login_txn``
tables via ``MemoryRepository``) into four operations:

- ``begin_login``    — mint ``state`` + PKCE + nonce, persist a short-TTL
                       ``login_txn``, return the OpenEMR authorize URL.
- ``complete_login`` — validate/consume the ``login_txn``, exchange the code
                       (PKCE + confidential secret), resolve identity, and create
                       the session with ENCRYPTED tokens.
- ``logout``         — mark the session revoked and best-effort revoke at
                       OpenEMR.
- ``resolve_session``— load by ``sha256(cookie)``, enforce idle + absolute TTL
                       (sliding), return the clinician + session view or ``None``.

All DB access goes through an injected ``session_scope_factory`` (defaults to the
real one) so the whole service is testable against in-memory SQLite. Nothing
here runs while ``auth_mode="disabled"`` — the routes gate on the mode.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import partial
from urllib.parse import urlencode, urlsplit

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from copilot.auth.identity import IdentityError, parse_identity, resolve_clinician
from copilot.auth.session import (
    SessionCrypto,
    SessionCryptoError,
    derive_csrf_token,
    ensure_utc,
    generate_session_id,
    hash_session_id,
)
from copilot.config import Settings
from copilot.domain.primitives import ClinicianId
from copilot.fhir.auth import (
    OAuthToken,
    SessionTokenProvider,
    SmartAppLaunchTokenProvider,
    TokenAcquisitionError,
)
from copilot.memory.db import session_scope
from copilot.memory.repository import MemoryRepository

_logger = logging.getLogger(__name__)

#: The BFF callback path, appended to ``public_base_url`` for the redirect_uri.
CALLBACK_PATH = "/v1/auth/callback"

#: A login transaction is single-use and short-lived — a physician who does not
#: complete the OpenEMR consent within this window must restart.
_LOGIN_TXN_TTL = timedelta(minutes=10)

SessionScopeFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


class AuthConfigError(RuntimeError):
    """SMART is enabled but the deployment is misconfigured (e.g. plain-HTTP origin)."""


class LoginCallbackError(RuntimeError):
    """A recoverable callback failure (bad/expired state, code-exchange failure).

    The route maps this to a generic ``/?login_error=…`` redirect — never a 500,
    and never leaking the internal reason.
    """


@dataclass(frozen=True)
class BeginLogin:
    """Result of ``begin_login`` — where to send the browser."""

    authorize_url: str
    state: str


@dataclass(frozen=True)
class LoginResult:
    """Result of a successful ``complete_login``."""

    cookie_value: str
    clinician_id: ClinicianId
    fhir_user: str
    redirect_target: str
    max_age: int


@dataclass(frozen=True)
class ResolvedSession:
    """A live, non-expired session resolved from a cookie."""

    clinician_id: ClinicianId
    session_id_hash: str
    fhir_user: str
    display_name: str | None
    expires_at: datetime
    csrf_token: str


def ensure_smart_ready(settings: Settings) -> None:
    """Refuse ``auth_mode="smart"`` on an unsafe/incomplete configuration.

    Per ``PRODUCTION_GRADE_PLAN.md`` §11: ``Secure`` cookies + OAuth-over-TLS
    require an https origin, so SMART cannot be enabled without one. Also requires
    the encryption key and the login client id. A no-op when auth is disabled.
    """
    if settings.auth_mode != "smart":
        return
    if not settings.public_base_url.lower().startswith("https://"):
        raise AuthConfigError(
            "auth_mode=smart requires an https public_base_url "
            "(Secure cookies + OAuth-over-TLS); refusing to start the login flow"
        )
    if not settings.session_enc_key:
        raise AuthConfigError("auth_mode=smart requires session_enc_key (token encryption at rest)")
    if not settings.smart_app_client_id:
        raise AuthConfigError("auth_mode=smart requires smart_app_client_id")


def _pkce_challenge(verifier: str) -> str:
    """S256 code_challenge = base64url(sha256(verifier)), no padding (RFC 7636)."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


@dataclass
class AuthService:
    """Stateless orchestrator; all state lives in the DB via the repository."""

    settings: Settings
    session_scope_factory: SessionScopeFactory = session_scope
    http_client_factory: Callable[..., httpx.AsyncClient] | None = None
    now_factory: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    state_factory: Callable[[], str] = field(default=lambda: secrets.token_urlsafe(32))
    verifier_factory: Callable[[], str] = field(default=lambda: secrets.token_urlsafe(64))
    nonce_factory: Callable[[], str] = field(default=lambda: secrets.token_urlsafe(16))

    # --- helpers ----------------------------------------------------------

    def _http(self) -> Callable[..., httpx.AsyncClient]:
        return self.http_client_factory or partial(
            httpx.AsyncClient, verify=self.settings.tls_verify
        )

    def _crypto(self) -> SessionCrypto:
        return SessionCrypto.from_key(self.settings.session_enc_key)

    @property
    def redirect_uri(self) -> str:
        return f"{self.settings.public_base_url.rstrip('/')}{CALLBACK_PATH}"

    def _public_fhir_aud(self) -> str:
        """The public FHIR base OpenEMR expects as the authorize ``aud``.

        OpenEMR validates the ``aud`` against the FHIR base it advertises
        publicly (derived from ``site_addr_oath``), which is the public origin —
        not the agent's internal read URL. In smart mode ``public_base_url`` is a
        validated https origin (see :func:`ensure_smart_ready`); combine it with
        the FHIR path from ``fhir_base_url`` so the ``aud`` matches while the
        actual FHIR reads keep using the internal ``fhir_base_url`` client.
        """
        fhir_path = urlsplit(self.settings.fhir_base_url).path or "/apis/default/fhir"
        return f"{self.settings.public_base_url.rstrip('/')}{fhir_path}"

    def _authorize_url(self, *, state: str, challenge: str, nonce: str) -> str:
        params = {
            "response_type": "code",
            "client_id": self.settings.smart_app_client_id,
            "redirect_uri": self.redirect_uri,
            "scope": self.settings.smart_scopes,
            "state": state,
            # Standalone launch: aud is the FHIR base as the authorization server
            # advertises it PUBLICLY (site_addr_oath-derived), not the internal
            # read URL — OpenEMR rejects a mismatched aud with invalid_request.
            "aud": self._public_fhir_aud(),
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "nonce": nonce,
        }
        sep = "&" if "?" in self.settings.oauth_authorize_url else "?"
        return f"{self.settings.oauth_authorize_url}{sep}{urlencode(params)}"

    def _revoke_url(self) -> str:
        base = self.settings.oauth_token_url.rstrip("/")
        if base.endswith("/token"):
            return base[: -len("/token")] + "/revoke"
        return base + "/revoke"

    # --- operations -------------------------------------------------------

    async def begin_login(self, redirect_target: str | None = None) -> BeginLogin:
        """Mint state + PKCE, persist the login_txn, return the authorize URL."""
        ensure_smart_ready(self.settings)
        now = self.now_factory()
        state = self.state_factory()
        verifier = self.verifier_factory()
        nonce = self.nonce_factory()
        challenge = _pkce_challenge(verifier)

        async with self.session_scope_factory() as session:
            await MemoryRepository(session).create_login_txn(
                state=state,
                code_verifier=verifier,
                nonce=nonce,
                redirect_target=redirect_target,
                created_at=now,
                expires_at=now + _LOGIN_TXN_TTL,
            )

        return BeginLogin(
            authorize_url=self._authorize_url(state=state, challenge=challenge, nonce=nonce),
            state=state,
        )

    async def complete_login(self, code: str, state: str) -> LoginResult:
        """Validate+consume the login_txn, exchange the code, create the session."""
        ensure_smart_ready(self.settings)
        now = self.now_factory()

        async with self.session_scope_factory() as session:
            txn = await MemoryRepository(session).consume_login_txn(state)
            if txn is None:
                raise LoginCallbackError("unknown or already-used login state")
            if ensure_utc(txn.expires_at) < now:
                raise LoginCallbackError("login transaction expired")
            verifier = txn.code_verifier
            redirect_target = txn.redirect_target or "/"

        token = await self._exchange_code(code, verifier)
        try:
            identity = parse_identity(id_token=token.id_token)
        except IdentityError as exc:
            raise LoginCallbackError("identity could not be resolved") from exc

        crypto = self._crypto()
        cookie_value = generate_session_id()
        session_hash = hash_session_id(cookie_value)
        access_enc = crypto.encrypt(token.access_token)
        refresh_enc = crypto.encrypt(token.refresh_token) if token.refresh_token else None
        absolute_expires_at = now + timedelta(seconds=self.settings.session_absolute_seconds)

        async with self.session_scope_factory() as session:
            repo = MemoryRepository(session)
            clinician_id = await resolve_clinician(repo, identity, now=now)
            await repo.create_physician_session(
                session_id=session_hash,
                clinician_id=clinician_id.value,
                access_token_enc=access_enc,
                refresh_token_enc=refresh_enc,
                access_expires_at=ensure_utc(token.expires_at),
                scope=token.scope,
                fhir_user=identity.fhir_user,
                created_at=now,
                absolute_expires_at=absolute_expires_at,
            )

        return LoginResult(
            cookie_value=cookie_value,
            clinician_id=clinician_id,
            fhir_user=identity.fhir_user,
            redirect_target=redirect_target,
            max_age=self.settings.session_idle_seconds,
        )

    async def _exchange_code(self, code: str, verifier: str) -> OAuthToken:
        provider = SmartAppLaunchTokenProvider(
            token_url=self.settings.oauth_token_url,
            client_id=self.settings.smart_app_client_id,
            redirect_uri=self.redirect_uri,
            authorization_code=code,
            client_secret=self.settings.smart_app_client_secret or None,
            code_verifier=verifier,
            http_client_factory=self._http(),
        )
        try:
            return await provider.get_token()
        except TokenAcquisitionError as exc:
            raise LoginCallbackError("authorization code exchange failed") from exc

    async def logout(self, cookie_value: str) -> None:
        """Mark the session revoked and best-effort revoke both tokens at OpenEMR."""
        session_hash = hash_session_id(cookie_value)
        crypto = self._crypto()
        access_plain: str | None = None
        refresh_plain: str | None = None

        async with self.session_scope_factory() as session:
            repo = MemoryRepository(session)
            row = await repo.get_physician_session(session_hash)
            if row is None:
                return
            try:
                access_plain = crypto.decrypt(row.access_token_enc)
                if row.refresh_token_enc is not None:
                    refresh_plain = crypto.decrypt(row.refresh_token_enc)
            except SessionCryptoError:
                # A key rotation can make old ciphertext undecryptable; we can no
                # longer revoke upstream, but we still revoke the local session.
                access_plain = refresh_plain = None
            await repo.revoke_physician_session(session_hash)

        await self._best_effort_revoke(access_plain, refresh_plain)

    async def _best_effort_revoke(self, *tokens: str | None) -> None:
        present = [t for t in tokens if t]
        if not present:
            return
        revoke_url = self._revoke_url()
        try:
            async with self._http()(timeout=10.0) as client:
                for tok in present:
                    data = {"token": tok, "client_id": self.settings.smart_app_client_id}
                    if self.settings.smart_app_client_secret:
                        data["client_secret"] = self.settings.smart_app_client_secret
                    await client.post(revoke_url, data=data)
        except Exception:
            # Best-effort only — the session is already revoked locally. Never
            # raise (logout must always succeed) and never log the token itself.
            _logger.warning("best-effort OpenEMR token revocation failed")

    async def resolve_session(self, cookie_value: str) -> ResolvedSession | None:
        """Resolve a cookie to a live session, enforcing idle + absolute TTL.

        Returns ``None`` (not an error) for an absent/revoked/expired session so
        the caller can respond 401 without leaking why.
        """
        ensure_smart_ready(self.settings)
        now = self.now_factory()
        session_hash = hash_session_id(cookie_value)

        async with self.session_scope_factory() as session:
            repo = MemoryRepository(session)
            row = await repo.get_physician_session(session_hash)
            if row is None or row.revoked:
                return None
            absolute = ensure_utc(row.absolute_expires_at)
            idle_deadline = ensure_utc(row.last_used_at) + timedelta(
                seconds=self.settings.session_idle_seconds
            )
            if now >= absolute or now >= idle_deadline:
                return None
            clinician_id = ClinicianId(value=row.clinician_id)
            fhir_user = row.fhir_user
            # Sliding-window refresh on activity.
            await repo.touch_physician_session(session_hash, now)
            clinician = await repo.get_clinician_by_fhir_user(fhir_user)
            display_name = clinician.display_name if clinician is not None else None

        # Effective expiry = whichever of the two deadlines comes first, post-slide.
        effective = min(absolute, now + timedelta(seconds=self.settings.session_idle_seconds))
        return ResolvedSession(
            clinician_id=clinician_id,
            session_id_hash=session_hash,
            fhir_user=fhir_user,
            display_name=display_name,
            expires_at=effective,
            csrf_token=derive_csrf_token(session_hash, self.settings.session_enc_key),
        )


# --- SessionTokenProvider wiring (consumed by the Phase-2 route cutover) ------


def make_session_token_io(
    settings: Settings,
    session_id_hash: str,
    *,
    session_scope_factory: SessionScopeFactory = session_scope,
) -> tuple[Callable[[], Awaitable[OAuthToken | None]], Callable[[OAuthToken], Awaitable[None]]]:
    """Build the (load, save) callables that back a ``SessionTokenProvider``.

    ``load`` decrypts the session's current token; ``save`` encrypts and persists
    a rotated token back to the row. Keeping crypto + DB here means
    ``SessionTokenProvider`` (in ``fhir.auth``) stays free of persistence imports.
    """
    crypto = SessionCrypto.from_key(settings.session_enc_key)

    async def load() -> OAuthToken | None:
        async with session_scope_factory() as session:
            row = await MemoryRepository(session).get_physician_session(session_id_hash)
            if row is None or row.revoked:
                return None
            access = crypto.decrypt(row.access_token_enc)
            refresh = crypto.decrypt(row.refresh_token_enc) if row.refresh_token_enc else None
            return OAuthToken(
                access_token=access,
                token_type="Bearer",
                expires_at=ensure_utc(row.access_expires_at),
                refresh_token=refresh,
                scope=row.scope,
            )

    async def save(token: OAuthToken) -> None:
        access_enc = crypto.encrypt(token.access_token)
        refresh_enc = crypto.encrypt(token.refresh_token) if token.refresh_token else None
        async with session_scope_factory() as session:
            await MemoryRepository(session).rotate_physician_session_token(
                session_id_hash,
                access_token_enc=access_enc,
                refresh_token_enc=refresh_enc,
                access_expires_at=ensure_utc(token.expires_at),
                scope=token.scope,
            )

    return load, save


def build_session_token_provider(
    settings: Settings,
    session_id_hash: str,
    *,
    session_scope_factory: SessionScopeFactory = session_scope,
    http_client_factory: Callable[..., httpx.AsyncClient] | None = None,
) -> SessionTokenProvider:
    """A ``SessionTokenProvider`` wired to the DB-backed, encrypted token store."""
    load, save = make_session_token_io(
        settings, session_id_hash, session_scope_factory=session_scope_factory
    )
    return SessionTokenProvider(
        token_url=settings.oauth_token_url,
        client_id=settings.smart_app_client_id,
        load_token=load,
        save_token=save,
        client_secret=settings.smart_app_client_secret or None,
        http_client_factory=http_client_factory
        or partial(httpx.AsyncClient, verify=settings.tls_verify),
    )
