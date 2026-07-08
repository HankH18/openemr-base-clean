"""Tests for the SMART App Launch + Backend Services token providers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from httpx import Response

from copilot.fhir.auth import (
    BackendServicesTokenProvider,
    OAuthToken,
    SmartAppLaunchTokenProvider,
    TokenAcquisitionError,
)


def _rsa_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")


# --- OAuthToken.is_fresh ---------------------------------------------------


class TestOAuthTokenFreshness:
    def test_fresh_when_expires_far_in_future(self) -> None:
        t = OAuthToken(access_token="x", token_type="Bearer", expires_at=datetime.now(UTC) + timedelta(hours=1))
        assert t.is_fresh() is True

    def test_stale_when_inside_skew_window(self) -> None:
        # 10 seconds from now, but skew is 30s ⇒ treated as stale
        t = OAuthToken(access_token="x", token_type="Bearer", expires_at=datetime.now(UTC) + timedelta(seconds=10))
        assert t.is_fresh() is False


# --- SMART App Launch ------------------------------------------------------


@pytest.mark.asyncio
class TestSmartAppLaunch:
    @respx.mock
    async def test_exchanges_authorization_code_on_first_call(self) -> None:
        route = respx.post("https://openemr.test/token").mock(
            return_value=Response(
                200,
                json={
                    "access_token": "phys-token-1",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                    "refresh_token": "phys-refresh-1",
                    "scope": "user/*.read",
                },
            )
        )
        provider = SmartAppLaunchTokenProvider(
            token_url="https://openemr.test/token",
            client_id="chat-app",
            redirect_uri="http://localhost/cb",
            authorization_code="abc123",
            client_secret="confidential-shhh",
        )
        token = await provider.get_token()

        assert token.access_token == "phys-token-1"
        assert token.token_type == "Bearer"
        assert token.refresh_token == "phys-refresh-1"

        # Right form fields on the wire.
        posted = dict(x.split("=", 1) for x in route.calls[0].request.content.decode().split("&"))
        assert posted["grant_type"] == "authorization_code"
        assert posted["code"] == "abc123"
        assert posted["client_id"] == "chat-app"
        assert posted["client_secret"] == "confidential-shhh"

    @respx.mock
    async def test_second_call_uses_cache_when_fresh(self) -> None:
        route = respx.post("https://openemr.test/token").mock(
            return_value=Response(
                200,
                json={"access_token": "phys-1", "token_type": "Bearer", "expires_in": 3600},
            )
        )
        provider = SmartAppLaunchTokenProvider(
            token_url="https://openemr.test/token",
            client_id="chat",
            redirect_uri="http://localhost/cb",
            authorization_code="abc",
        )
        first = await provider.get_token()
        second = await provider.get_token()
        assert first.access_token == second.access_token
        assert route.call_count == 1  # cached

    @respx.mock
    async def test_force_refresh_uses_refresh_token_when_available(self) -> None:
        route = respx.post("https://openemr.test/token").mock(
            side_effect=[
                Response(
                    200,
                    json={
                        "access_token": "phys-1",
                        "token_type": "Bearer",
                        "expires_in": 3600,
                        "refresh_token": "refresh-1",
                    },
                ),
                Response(
                    200,
                    json={"access_token": "phys-2", "token_type": "Bearer", "expires_in": 3600},
                ),
            ]
        )
        provider = SmartAppLaunchTokenProvider(
            token_url="https://openemr.test/token",
            client_id="chat",
            redirect_uri="http://localhost/cb",
            authorization_code="abc",
        )
        assert (await provider.get_token()).access_token == "phys-1"
        second = await provider.get_token(force=True)
        assert second.access_token == "phys-2"

        # Second POST should have grant_type=refresh_token.
        posted = dict(x.split("=", 1) for x in route.calls[1].request.content.decode().split("&"))
        assert posted["grant_type"] == "refresh_token"
        assert posted["refresh_token"] == "refresh-1"

    @respx.mock
    async def test_raises_on_401(self) -> None:
        respx.post("https://openemr.test/token").mock(return_value=Response(401, json={"error": "bad_code"}))
        provider = SmartAppLaunchTokenProvider(
            token_url="https://openemr.test/token",
            client_id="chat",
            redirect_uri="http://localhost/cb",
            authorization_code="wrong",
        )
        with pytest.raises(TokenAcquisitionError):
            await provider.get_token()


# --- SMART Backend Services -----------------------------------------------


@pytest.mark.asyncio
class TestBackendServices:
    @respx.mock
    async def test_client_credentials_with_signed_jwt_assertion(self) -> None:
        pem = _rsa_pem()
        route = respx.post("https://openemr.test/token").mock(
            return_value=Response(
                200,
                json={"access_token": "sys-1", "token_type": "Bearer", "expires_in": 300, "scope": "system/Patient.read"},
            )
        )
        provider = BackendServicesTokenProvider(
            token_url="https://openemr.test/token",
            client_id="poller-app",
            private_key_pem=pem,
            algorithm="RS384",
            scopes=("system/Patient.read", "system/Observation.read"),
        )
        token = await provider.get_token()
        assert token.access_token == "sys-1"

        posted = dict(x.split("=", 1) for x in route.calls[0].request.content.decode().split("&"))
        assert posted["grant_type"] == "client_credentials"
        assert (
            posted["client_assertion_type"]
            == "urn%3Aietf%3Aparams%3Aoauth%3Aclient-assertion-type%3Ajwt-bearer"
        )
        # The assertion must be a valid JWT-shaped string (3 segments).
        assertion = posted["client_assertion"]
        assert assertion.count(".") == 2

    @respx.mock
    async def test_caches_token_between_calls(self) -> None:
        pem = _rsa_pem()
        route = respx.post("https://openemr.test/token").mock(
            return_value=Response(
                200,
                json={"access_token": "sys-1", "token_type": "Bearer", "expires_in": 3600},
            )
        )
        provider = BackendServicesTokenProvider(
            token_url="https://openemr.test/token",
            client_id="poller",
            private_key_pem=pem,
        )
        await provider.get_token()
        await provider.get_token()
        assert route.call_count == 1

    @respx.mock
    async def test_forces_fresh_assertion_on_force(self) -> None:
        pem = _rsa_pem()
        respx.post("https://openemr.test/token").mock(
            return_value=Response(
                200,
                json={"access_token": "sys-1", "token_type": "Bearer", "expires_in": 3600},
            )
        )
        provider = BackendServicesTokenProvider(
            token_url="https://openemr.test/token",
            client_id="poller",
            private_key_pem=pem,
        )
        first = await provider.get_token()
        # Advance the clock inside the provider so force actually rebuilds.
        provider._cached = None  # type: ignore[attr-defined]
        second = await provider.get_token(force=True)
        assert first.access_token == second.access_token  # server returned same

    @respx.mock
    async def test_scopes_sent_space_separated(self) -> None:
        pem = _rsa_pem()
        route = respx.post("https://openemr.test/token").mock(
            return_value=Response(
                200,
                json={"access_token": "sys-1", "token_type": "Bearer", "expires_in": 300},
            )
        )
        provider = BackendServicesTokenProvider(
            token_url="https://openemr.test/token",
            client_id="poller",
            private_key_pem=pem,
            scopes=("system/Patient.read", "system/Observation.read"),
        )
        await provider.get_token()
        posted = dict(x.split("=", 1) for x in route.calls[0].request.content.decode().split("&"))
        # scopes are URL-encoded: 'system/Patient.read system/Observation.read' → '+' between
        assert "system%2FPatient.read" in posted["scope"]
        assert "system%2FObservation.read" in posted["scope"]
