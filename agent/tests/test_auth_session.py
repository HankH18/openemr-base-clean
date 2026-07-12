"""Crypto + cookie + session-id primitives for SMART login (``copilot.auth.session``)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from cryptography.fernet import Fernet
from starlette.responses import Response

from copilot.auth.session import (
    SessionCrypto,
    SessionCryptoError,
    clear_session_cookie,
    derive_csrf_token,
    ensure_utc,
    generate_session_id,
    hash_session_id,
    read_session_cookie,
    set_session_cookie,
)


class TestSessionCrypto:
    def test_encrypt_decrypt_roundtrip(self) -> None:
        crypto = SessionCrypto.from_key(Fernet.generate_key().decode())
        ciphertext = crypto.encrypt("phys-access-token")
        assert isinstance(ciphertext, bytes)
        assert b"phys-access-token" not in ciphertext  # actually encrypted
        assert crypto.decrypt(ciphertext) == "phys-access-token"

    def test_two_keys_cannot_read_each_others_ciphertext(self) -> None:
        a = SessionCrypto.from_key(Fernet.generate_key().decode())
        b = SessionCrypto.from_key(Fernet.generate_key().decode())
        ciphertext = a.encrypt("secret")
        with pytest.raises(SessionCryptoError):
            b.decrypt(ciphertext)

    def test_empty_key_is_rejected(self) -> None:
        with pytest.raises(SessionCryptoError):
            SessionCrypto.from_key("")

    def test_invalid_key_is_rejected(self) -> None:
        with pytest.raises(SessionCryptoError):
            SessionCrypto.from_key("not-a-valid-fernet-key")

    def test_decrypt_garbage_raises(self) -> None:
        crypto = SessionCrypto.from_key(Fernet.generate_key().decode())
        with pytest.raises(SessionCryptoError):
            crypto.decrypt(b"\x00\x01not-ciphertext")


class TestSessionId:
    def test_generate_is_high_entropy_and_unique(self) -> None:
        a = generate_session_id()
        b = generate_session_id()
        assert a != b
        assert len(a) >= 40  # token_urlsafe(32) ~ 43 chars

    def test_hash_is_stable_sha256_hex(self) -> None:
        cookie = "opaque-cookie-value"
        h1 = hash_session_id(cookie)
        h2 = hash_session_id(cookie)
        assert h1 == h2
        assert len(h1) == 64
        # The stored hash is not the plaintext cookie.
        assert h1 != cookie

    def test_csrf_token_is_bound_to_session_and_secret(self) -> None:
        session_hash = hash_session_id("cookie")
        t1 = derive_csrf_token(session_hash, "secret-a")
        assert t1 == derive_csrf_token(session_hash, "secret-a")  # stable
        assert t1 != derive_csrf_token(session_hash, "secret-b")  # keyed by secret
        assert t1 != derive_csrf_token(hash_session_id("other"), "secret-a")  # keyed by session


class TestEnsureUtc:
    def test_naive_is_treated_as_utc(self) -> None:
        naive = datetime(2026, 7, 11, 12, 0, 0)
        out = ensure_utc(naive)
        assert out.tzinfo == UTC
        assert out.hour == 12

    def test_aware_non_utc_is_converted(self) -> None:
        aware = datetime(2026, 7, 11, 12, 0, 0, tzinfo=timezone(timedelta(hours=5)))
        out = ensure_utc(aware)
        assert out.tzinfo == UTC
        assert out.hour == 7  # 12:00+05:00 == 07:00 UTC


class TestCookies:
    def test_set_session_cookie_has_security_attributes(self) -> None:
        response = Response()
        set_session_cookie(response, name="af_session", value="abc123", max_age=1800)
        header = response.headers.get("set-cookie")
        assert header is not None
        assert "af_session=abc123" in header
        assert "HttpOnly" in header
        assert "Secure" in header
        assert "Path=/" in header
        assert "Max-Age=1800" in header
        assert "samesite=lax" in header.lower()

    def test_secure_can_be_disabled_for_local_http(self) -> None:
        response = Response()
        set_session_cookie(response, name="af_session", value="x", max_age=10, secure=False)
        assert "Secure" not in response.headers.get("set-cookie", "")

    def test_clear_session_cookie_expires_it(self) -> None:
        response = Response()
        clear_session_cookie(response, name="af_session")
        header = response.headers.get("set-cookie", "")
        assert "af_session=" in header
        assert "Max-Age=0" in header

    def test_read_session_cookie(self) -> None:
        assert read_session_cookie({"af_session": "v"}, "af_session") == "v"
        assert read_session_cookie({}, "af_session") is None
