"""Session crypto + cookie primitives for per-physician SMART login.

The BFF (see ``PRODUCTION_GRADE_PLAN.md`` §1) is the sole holder of the
physician's OpenEMR token. This module provides the low-level, framework-light
building blocks the ``AuthService`` and the ``/v1/auth/*`` routes compose:

- **Token encryption at rest** — ``SessionCrypto`` (Fernet) encrypts/decrypts the
  access + refresh tokens so ``physician_session`` never stores plaintext.
- **Opaque session id** — ``generate_session_id`` mints the cookie value; the DB
  stores only ``hash_session_id`` (sha256) of it, so a DB leak yields no live
  cookies.
- **Cookie build/clear** — ``HttpOnly; Secure; SameSite=Lax; Path=/`` in one
  place, so no route hand-rolls attributes and forgets ``HttpOnly``.
- **CSRF** — ``derive_csrf_token`` is a stateless double-submit token bound to
  the session (HMAC), returned by ``/v1/auth/me`` for the SPA to echo.

Hard rule (ARCHITECTURE §Security): tokens, cookie values, and the encryption
key are never logged.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from cryptography.fernet import Fernet, InvalidToken
from starlette.responses import Response

# Cookie attributes are fixed by the BFF design; only name/value/max-age vary.
_COOKIE_PATH = "/"
_COOKIE_SAMESITE: Literal["lax"] = "lax"


class SessionCryptoError(RuntimeError):
    """Encryption key is missing/invalid, or a ciphertext could not be decrypted."""


@dataclass(frozen=True)
class SessionCrypto:
    """Fernet wrapper that encrypts token strings for storage at rest."""

    _fernet: Fernet

    @classmethod
    def from_key(cls, key: str) -> SessionCrypto:
        """Build from a urlsafe-base64 32-byte Fernet key (``session_enc_key``)."""
        if not key:
            raise SessionCryptoError("session encryption key is not configured")
        try:
            return cls(Fernet(key.encode("ascii")))
        except (ValueError, TypeError) as exc:
            raise SessionCryptoError("session encryption key is not a valid Fernet key") from exc

    def encrypt(self, plaintext: str) -> bytes:
        """Encrypt a token string to opaque ciphertext bytes."""
        return self._fernet.encrypt(plaintext.encode("utf-8"))

    def decrypt(self, ciphertext: bytes) -> str:
        """Decrypt ciphertext bytes back to the token string."""
        try:
            return self._fernet.decrypt(ciphertext).decode("utf-8")
        except InvalidToken as exc:
            raise SessionCryptoError("could not decrypt session token") from exc


def generate_session_id() -> str:
    """Mint the opaque cookie value (~256 bits of entropy)."""
    return secrets.token_urlsafe(32)


def hash_session_id(cookie_value: str) -> str:
    """sha256 hex of the cookie value — the stored PK, never the plaintext."""
    return hashlib.sha256(cookie_value.encode("utf-8")).hexdigest()


def derive_csrf_token(session_id_hash: str, secret: str) -> str:
    """Stateless double-submit CSRF token bound to the session.

    HMAC-SHA256 over the stored session id hash keyed by the app secret
    (``session_enc_key``), so it is stable for the life of a session and
    verifiable without a DB round-trip. Not enforced by any route in Phase 1;
    returned by ``/v1/auth/me`` so the SPA can echo it on POSTs once the cutover
    lands (defense-in-depth atop same-origin + ``SameSite=Lax``).
    """
    return hmac.new(
        secret.encode("utf-8"), session_id_hash.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def ensure_utc(value: datetime) -> datetime:
    """Normalize a possibly-naive DB datetime to timezone-aware UTC.

    SQLite has no tz support and returns naive datetimes even from
    ``DateTime(timezone=True)`` columns; Postgres round-trips aware. Callers must
    normalize before comparing against ``datetime.now(UTC)`` (a naive/aware
    comparison raises ``TypeError``).
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def set_session_cookie(
    response: Response,
    *,
    name: str,
    value: str,
    max_age: int,
    secure: bool = True,
) -> None:
    """Attach the opaque session cookie: ``HttpOnly; Secure; SameSite=Lax; Path=/``."""
    response.set_cookie(
        key=name,
        value=value,
        max_age=max_age,
        path=_COOKIE_PATH,
        secure=secure,
        httponly=True,
        samesite=_COOKIE_SAMESITE,
    )


def clear_session_cookie(response: Response, *, name: str, secure: bool = True) -> None:
    """Expire the session cookie with the same attributes it was set with."""
    response.delete_cookie(
        key=name,
        path=_COOKIE_PATH,
        secure=secure,
        httponly=True,
        samesite=_COOKIE_SAMESITE,
    )


def read_session_cookie(cookies: Mapping[str, str], name: str) -> str | None:
    """Read the session cookie value from a request's parsed cookie mapping."""
    return cookies.get(name)
