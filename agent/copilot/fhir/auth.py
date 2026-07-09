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
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol

import httpx
from authlib.jose import jwt as jose_jwt

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
    http_client_factory: type[httpx.AsyncClient] = field(default=httpx.AsyncClient)
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
        async with self.http_client_factory(timeout=10.0) as client:
            resp = await client.post(self.token_url, data=data)
        return _parse_token_response(resp)

    async def _refresh(self, refresh_token: str) -> OAuthToken:
        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": self.client_id,
        }
        if self.client_secret:
            data["client_secret"] = self.client_secret
        async with self.http_client_factory(timeout=10.0) as client:
            resp = await client.post(self.token_url, data=data)
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
    http_client_factory: type[httpx.AsyncClient] = field(default=httpx.AsyncClient)
    jti_factory: callable = field(default=lambda: secrets.token_urlsafe(16))  # type: ignore[assignment]
    now_factory: callable = field(default=lambda: datetime.now(UTC))  # type: ignore[assignment]
    _cached: OAuthToken | None = field(default=None, init=False, repr=False)

    async def get_token(self, force: bool = False) -> OAuthToken:
        if not force and self._cached is not None and self._cached.is_fresh():
            return self._cached
        assertion = self._build_assertion()
        data = {
            "grant_type": "client_credentials",
            "scope": " ".join(self.scopes),
            "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
            "client_assertion": assertion,
        }
        async with self.http_client_factory(timeout=10.0) as client:
            resp = await client.post(self.token_url, data=data)
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
        return jose_jwt.encode(header, payload, self.private_key_pem).decode("ascii")


# --- helpers ----------------------------------------------------------------


class TokenAcquisitionError(Exception):
    """Raised when a token endpoint returns non-2xx or a malformed body."""


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
    )
