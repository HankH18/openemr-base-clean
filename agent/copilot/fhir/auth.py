"""OAuth token providers.

Two SMART-on-FHIR profiles:

- ``SmartAppLaunchTokenProvider`` — physician-delegated ``authorization_code``
  grant. The caller (chat entry point) receives the code from the browser
  after the SMART launch; the provider exchanges it and refreshes as
  needed.  Used for **all** interactive chat reads so OpenEMR enforces
  which patients that physician may see.

- ``BackendServicesTokenProvider`` — ``client_credentials`` grant with a
  signed JWT ``client_assertion`` (``private_key_jwt``).  Scopes: minimal
  ``system/*.read``. Used by the background poller only.

Both providers cache the acquired token in-memory and refetch when it is
about to expire (or on an explicit ``force`` after 401).

Design note: token providers do **not** log the tokens themselves.  Only
IDs and expiry seconds are ever logged.  This is a hard rule (see
ARCHITECTURE §Security).

Retry note: every grant here is retried on transient transport failures
(:data:`copilot.resilience.DEFAULT_RETRY` — 3 bounded, jittered attempts on
timeouts/connection errors/429/5xx, never on any other 4xx). See
:func:`_post_token` for why that is safe even for the single-use
``authorization_code`` and rotating ``refresh_token`` grants.
"""

from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Protocol, cast

import httpx
from authlib.jose import jwt as jose_jwt  # type: ignore[import-untyped]  # authlib ships no stubs

from copilot.resilience import (
    DEFAULT_RETRY,
    TOKEN_TIMEOUT,
    RetryPolicy,
    retry_async,
    retryable_response,
)

# 30-second skew guard on token expiry — refresh a bit before the clock says.
_EXPIRY_SKEW = timedelta(seconds=30)


@dataclass(frozen=True)
class OAuthToken:
    """One acquired access token + its expiry."""

    access_token: str
    token_type: str  # typically "Bearer"
    expires_at: datetime  # timezone-aware UTC
    refresh_token: str | None = None
    scope: str | None = None
    # OpenID Connect id_token from an authorization_code exchange (SMART login).
    # Present only on the login exchange; carries the fhirUser/sub identity
    # claims. Never populated for client_credentials/password grants.
    id_token: str | None = None

    def is_fresh(self, now: datetime | None = None) -> bool:
        """True while the token is still safely usable."""
        return (now or datetime.now(UTC)) + _EXPIRY_SKEW < self.expires_at


class TokenProvider(Protocol):
    """Contract for OAuth token acquisition — the FHIR client depends on this."""

    async def get_token(self, force: bool = False) -> OAuthToken:
        """Return a fresh token; refetch if ``force`` or expiry-imminent."""
        ...


@dataclass
class StaticTokenProvider:
    """Bake-in provider — for tests and short-lived scripts.

    Never used in production paths, but explicit here so the FHIR client
    can be exercised without spinning up a full OAuth mock.
    """

    token: OAuthToken

    async def get_token(self, force: bool = False) -> OAuthToken:
        return self.token


# --- SMART App Launch (physician-delegated) ---------------------------------


@dataclass
class SmartAppLaunchTokenProvider:
    """Exchange a browser-delivered auth code for a physician token.

    The caller is responsible for kicking off the browser redirect and
    receiving the ``code`` — that lives in the API layer.  This provider
    just does the token exchange + refresh.
    """

    token_url: str
    client_id: str
    redirect_uri: str
    authorization_code: str
    client_secret: str | None = None  # confidential clients only
    # PKCE (RFC 7636) verifier — sent on the code exchange when the authorize
    # request carried the matching S256 code_challenge. Optional and backward
    # compatible: pre-PKCE callers leave it None and the field is simply omitted.
    code_verifier: str | None = field(default=None, repr=False)
    http_client_factory: Callable[..., httpx.AsyncClient] = field(default=httpx.AsyncClient)
    retry: RetryPolicy = field(default=DEFAULT_RETRY)
    _cached: OAuthToken | None = field(default=None, init=False, repr=False)

    async def get_token(self, force: bool = False) -> OAuthToken:
        if not force and self._cached is not None and self._cached.is_fresh():
            return self._cached
        if self._cached is not None and self._cached.refresh_token:
            token = await self._refresh(self._cached.refresh_token)
        else:
            token = await self._exchange_code()
        self._cached = token
        return token

    async def _exchange_code(self) -> OAuthToken:
        data = {
            "grant_type": "authorization_code",
            "code": self.authorization_code,
            "redirect_uri": self.redirect_uri,
            "client_id": self.client_id,
        }
        if self.client_secret:
            data["client_secret"] = self.client_secret
        if self.code_verifier:
            data["code_verifier"] = self.code_verifier
        resp = await _post_token(
            self.http_client_factory, self.token_url, lambda: data, retry=self.retry
        )
        return _parse_token_response(resp)

    async def _refresh(self, refresh_token: str) -> OAuthToken:
        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": self.client_id,
        }
        if self.client_secret:
            data["client_secret"] = self.client_secret
        resp = await _post_token(
            self.http_client_factory, self.token_url, lambda: data, retry=self.retry
        )
        return _parse_token_response(resp)


# --- Session-backed (per-physician SMART login) -----------------------------


@dataclass
class SessionTokenProvider:
    """Serve a logged-in physician's token from an encrypted server session.

    Satisfies the :class:`TokenProvider` protocol so ``FhirClient`` /
    ``OpenEmrWriteClient`` consume it unchanged. Unlike the other providers it
    does not cache in-memory: the source of truth is the ``physician_session``
    row, so every request loads the current (decrypted) token via the injected
    ``load_token`` callable. When the cached access token is stale (or ``force``),
    it refreshes with the stored refresh token and **persists the rotated token
    back** via ``save_token`` — OpenEMR rotates refresh tokens, so the loser of a
    race must re-read rather than reuse.

    DB access is injected as two small awaitable callables (``load_token`` /
    ``save_token``) rather than importing a global session scope, keeping this
    module free of persistence imports and the refresh-rotation path unit
    testable without a database.
    """

    token_url: str
    client_id: str
    load_token: Callable[[], Awaitable[OAuthToken | None]]
    save_token: Callable[[OAuthToken], Awaitable[None]]
    client_secret: str | None = field(default=None, repr=False)
    http_client_factory: Callable[..., httpx.AsyncClient] = field(default=httpx.AsyncClient)
    retry: RetryPolicy = field(default=DEFAULT_RETRY)

    async def get_token(self, force: bool = False) -> OAuthToken:
        current = await self.load_token()
        if current is None:
            raise TokenAcquisitionError("no active physician session token")
        if not force and current.is_fresh():
            return current
        if not current.refresh_token:
            # Nothing to refresh with. If it is still (barely) fresh, hand it
            # back; otherwise the physician must re-authenticate.
            if current.is_fresh():
                return current
            raise TokenAcquisitionError("physician session token expired; no refresh token")
        refreshed = await self._refresh(current.refresh_token)
        # OpenEMR MAY omit a new refresh_token on rotation; keep the prior one so
        # a subsequent refresh still has a credential to present.
        if refreshed.refresh_token is None:
            refreshed = replace(refreshed, refresh_token=current.refresh_token)
        await self.save_token(refreshed)
        return refreshed

    async def _refresh(self, refresh_token: str) -> OAuthToken:
        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": self.client_id,
        }
        if self.client_secret:
            data["client_secret"] = self.client_secret
        resp = await _post_token(
            self.http_client_factory, self.token_url, lambda: data, retry=self.retry
        )
        return _parse_token_response(resp)


# --- Resource-Owner Password (dedicated write user) -------------------------


@dataclass
class ResourceOwnerPasswordTokenProvider:
    """``grant_type=password`` against a dedicated OpenEMR write user.

    Phase-1 write attribution: the Standard REST API exposes only ``user/``
    scopes and demands a user/ACL session, which the system read token cannot
    provide (see ``research/WRITEBACK_PHASE1_PLAN.md`` §2). A dedicated
    confidential client + ``copilot_writer`` user, exchanged via the
    resource-owner password grant, is the credential that can actually write.

    Mirrors ``SmartAppLaunchTokenProvider``: caches in memory, refreshes via the
    ``refresh_token`` when one is present, and reuses ``_parse_token_response``.
    The ``password`` and ``client_secret`` are held ``repr=False`` so they never
    surface in a stack trace or log line — the "never log secrets" rule extends
    to these higher-value writable credentials.
    """

    token_url: str
    client_id: str
    username: str
    password: str = field(repr=False)
    client_secret: str | None = field(default=None, repr=False)
    user_role: str = "users"
    scope: str | None = None
    http_client_factory: Callable[..., httpx.AsyncClient] = field(default=httpx.AsyncClient)
    retry: RetryPolicy = field(default=DEFAULT_RETRY)
    _cached: OAuthToken | None = field(default=None, init=False, repr=False)

    async def get_token(self, force: bool = False) -> OAuthToken:
        if not force and self._cached is not None and self._cached.is_fresh():
            return self._cached
        if self._cached is not None and self._cached.refresh_token:
            token = await self._refresh(self._cached.refresh_token)
        else:
            token = await self._password_grant()
        self._cached = token
        return token

    async def _password_grant(self) -> OAuthToken:
        data = {
            "grant_type": "password",
            "client_id": self.client_id,
            "user_role": self.user_role,
            "username": self.username,
            "password": self.password,
        }
        if self.scope:
            data["scope"] = self.scope
        if self.client_secret:
            data["client_secret"] = self.client_secret
        resp = await _post_token(
            self.http_client_factory, self.token_url, lambda: data, retry=self.retry
        )
        return _parse_token_response(resp)

    async def _refresh(self, refresh_token: str) -> OAuthToken:
        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": self.client_id,
        }
        if self.scope:
            data["scope"] = self.scope
        if self.client_secret:
            data["client_secret"] = self.client_secret
        resp = await _post_token(
            self.http_client_factory, self.token_url, lambda: data, retry=self.retry
        )
        return _parse_token_response(resp)


# --- SMART Backend Services (client_credentials + JWT assertion) ------------


@dataclass
class BackendServicesTokenProvider:
    """``client_credentials`` grant with a ``private_key_jwt`` assertion.

    SMART Backend Services mandates ``client_assertion_type=urn:ietf:
    params:oauth:client-assertion-type:jwt-bearer``.  We build the JWT
    with ``authlib.jose`` — supports RS384 and ES384 (the profile's
    required algs).
    """

    token_url: str
    client_id: str
    private_key_pem: str
    algorithm: str = "RS384"  # or "ES384"
    scopes: tuple[str, ...] = ("system/Patient.read",)
    audience: str | None = None  # defaults to token_url
    http_client_factory: Callable[..., httpx.AsyncClient] = field(default=httpx.AsyncClient)
    jti_factory: Callable[[], str] = field(default=lambda: secrets.token_urlsafe(16))
    now_factory: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    retry: RetryPolicy = field(default=DEFAULT_RETRY)
    _cached: OAuthToken | None = field(default=None, init=False, repr=False)

    async def get_token(self, force: bool = False) -> OAuthToken:
        if not force and self._cached is not None and self._cached.is_fresh():
            return self._cached

        def _grant() -> dict[str, str]:
            # Built PER ATTEMPT, not once: the assertion carries a single-use
            # ``jti`` and a short ``exp``, and SMART Backend Services servers are
            # entitled to reject a replayed jti. Re-minting makes each retry a
            # genuinely new, replay-safe request rather than a resend the server
            # is right to refuse — and keeps ``exp`` from expiring underneath a
            # backoff. It is the only reason ``_post_token`` takes a factory.
            return {
                "grant_type": "client_credentials",
                "scope": " ".join(self.scopes),
                "client_assertion_type": (
                    "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
                ),
                "client_assertion": self._build_assertion(),
            }

        resp = await _post_token(
            self.http_client_factory, self.token_url, _grant, retry=self.retry
        )
        token = _parse_token_response(resp)
        self._cached = token
        return token

    def _build_assertion(self) -> str:
        now = self.now_factory()
        aud = self.audience or self.token_url
        header = {"alg": self.algorithm, "typ": "JWT"}
        payload = {
            "iss": self.client_id,
            "sub": self.client_id,
            "aud": aud,
            "jti": self.jti_factory(),
            "exp": int((now + timedelta(minutes=5)).timestamp()),
            "iat": int(now.timestamp()),
        }
        return cast("str", jose_jwt.encode(header, payload, self.private_key_pem).decode("ascii"))


# --- helpers ----------------------------------------------------------------


class TokenAcquisitionError(Exception):
    """Raised when a token endpoint returns non-2xx or a malformed body."""


async def _post_token(
    http_client_factory: Callable[..., httpx.AsyncClient],
    token_url: str,
    data_factory: Callable[[], dict[str, str]],
    *,
    retry: RetryPolicy,
) -> httpx.Response:
    """POST one token grant, retrying only genuinely transient failures.

    **Why retrying a token grant is safe — including the single-use ones.**
    ``authorization_code`` codes are single-use and OpenEMR *rotates*
    ``refresh_token``s, so these grants are not idempotent in the usual sense.
    Retrying them is nonetheless provably not-worse-than-failing, which is the
    bar that matters:

    - If the request never reached the server, the credential is untouched and
      the retry is both safe and valuable — it saves a physician from being
      logged out by a one-off network blip mid-turn.
    - If it *did* reach the server and only the response was lost, the credential
      is already spent — by the first attempt, whether or not we retry. The retry
      then presents a spent credential, the server answers **4xx**
      ``invalid_grant``, and :func:`is_retryable_status` stops the loop dead. The
      caller gets the same :class:`TokenAcquisitionError` it would have got
      without any retry, and the physician re-authenticates either way.

    So the retry can only ever turn a failure into a success. Single-use is
    enforced *server-side* and reported as a 4xx we never retry — that
    server-side enforcement is the whole proof.

    **This argument does NOT extend to clinical writes**, and the difference is
    exactly the missing enforcement: a lost response on ``POST /patient/x/vital``
    means the row landed, the server will happily accept an identical second one,
    and a retry silently duplicates a clinical record. That is why
    ``copilot.fhir.write_client`` stays fail-closed and is untouched by this
    module. Retrying a clinical write is worse than failing it.

    ``data_factory`` is a callable rather than a dict so a grant whose body must
    be *freshly minted per attempt* can be — see
    :class:`BackendServicesTokenProvider`, whose JWT assertion carries a
    single-use ``jti``.
    """

    async def _attempt() -> httpx.Response:
        async with http_client_factory(timeout=TOKEN_TIMEOUT) as client:
            return await client.post(token_url, data=data_factory())

    return await retry_async(_attempt, policy=retry, should_retry_result=retryable_response)


def _parse_token_response(resp: httpx.Response) -> OAuthToken:
    if resp.status_code >= 400:
        raise TokenAcquisitionError(f"token endpoint returned status={resp.status_code}")
    body = resp.json()
    if not isinstance(body, dict) or "access_token" not in body:
        raise TokenAcquisitionError("token response missing access_token")
    expires_in = int(body.get("expires_in", 300))
    return OAuthToken(
        access_token=str(body["access_token"]),
        token_type=str(body.get("token_type", "Bearer")),
        expires_at=datetime.now(UTC) + timedelta(seconds=expires_in),
        refresh_token=body.get("refresh_token"),
        scope=body.get("scope"),
        id_token=body.get("id_token"),
    )
