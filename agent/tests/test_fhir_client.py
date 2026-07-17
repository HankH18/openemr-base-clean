"""Tests for the FHIR client (reads + change query + 401 retry)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import respx
from httpx import Response

from copilot.domain.primitives import PatientId, ResourceType
from copilot.fhir.auth import OAuthToken, StaticTokenProvider
from copilot.fhir.client import FhirClient, FhirClientError

pytestmark = pytest.mark.asyncio


def _tok(value: str = "abc") -> OAuthToken:
    return OAuthToken(
        access_token=value,
        token_type="Bearer",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


class TestRead:
    @respx.mock
    async def test_read_attaches_bearer_and_accept_header(self) -> None:
        route = respx.get("http://oe.test/fhir/Patient/1015").mock(
            return_value=Response(200, json={"resourceType": "Patient", "id": "1015"})
        )
        async with FhirClient(
            "http://oe.test/fhir", StaticTokenProvider(token=_tok("physician-jwt"))
        ) as client:
            body = await client.read(ResourceType.Patient, "1015")
        assert body["id"] == "1015"
        req = route.calls[0].request
        assert req.headers["Authorization"] == "Bearer physician-jwt"
        assert "fhir+json" in req.headers["Accept"]

    @respx.mock
    async def test_raises_on_non_2xx(self) -> None:
        respx.get("http://oe.test/fhir/Patient/9").mock(return_value=Response(404))
        async with FhirClient("http://oe.test/fhir", StaticTokenProvider(token=_tok())) as client:
            with pytest.raises(FhirClientError):
                await client.read(ResourceType.Patient, "9")


class TestSearch:
    @respx.mock
    async def test_search_returns_bundle_json(self) -> None:
        respx.get("http://oe.test/fhir/Observation").mock(
            return_value=Response(
                200,
                json={
                    "resourceType": "Bundle",
                    "type": "searchset",
                    "total": 3,
                    "entry": [],
                },
            )
        )
        async with FhirClient("http://oe.test/fhir", StaticTokenProvider(token=_tok())) as client:
            body = await client.search(
                ResourceType.Observation, {"patient": "1015", "_count": "50"}
            )
        assert body["total"] == 3


class TestCountSince:
    @respx.mock
    async def test_count_query_shape_and_return_value(self) -> None:
        # Two-call check: url composition + returned int.
        route = respx.get("http://oe.test/fhir/Observation").mock(
            return_value=Response(
                200, json={"resourceType": "Bundle", "type": "searchset", "total": 7}
            )
        )
        since = datetime(2026, 7, 8, 0, 0, tzinfo=UTC)
        async with FhirClient("http://oe.test/fhir", StaticTokenProvider(token=_tok())) as client:
            n = await client.count_since(ResourceType.Observation, PatientId(value=1015), since)
        assert n == 7

        params = dict(route.calls[0].request.url.params.multi_items())
        assert params["patient"] == "1015"
        assert params["_summary"] == "count"
        # `_lastUpdated=gt<iso-Z>` shape
        assert params["_lastUpdated"].startswith("gt2026-07-08T00:00:00")
        assert params["_lastUpdated"].endswith("Z")

    @respx.mock
    async def test_raises_when_total_missing(self) -> None:
        respx.get("http://oe.test/fhir/Observation").mock(
            return_value=Response(200, json={"resourceType": "Bundle"})
        )
        async with FhirClient("http://oe.test/fhir", StaticTokenProvider(token=_tok())) as client:
            with pytest.raises(FhirClientError):
                await client.count_since(
                    ResourceType.Observation, PatientId(value=1015), datetime.now(UTC)
                )


class TestUnauthorizedRetry:
    @respx.mock
    async def test_retries_once_with_forced_token_refresh_on_401(self) -> None:
        route = respx.get("http://oe.test/fhir/Patient/1015").mock(
            side_effect=[
                Response(401, json={"error": "expired"}),
                Response(200, json={"resourceType": "Patient", "id": "1015"}),
            ]
        )

        # Provider that returns a new access token on force=True — proves
        # the client asked for a fresh one.
        class RotatingProvider:
            def __init__(self) -> None:
                self.calls: list[bool] = []

            async def get_token(self, force: bool = False) -> OAuthToken:
                self.calls.append(force)
                return _tok(f"tok-{len(self.calls)}")

        provider = RotatingProvider()
        async with FhirClient("http://oe.test/fhir", provider) as client:  # type: ignore[arg-type]
            body = await client.read(ResourceType.Patient, "1015")

        assert body["id"] == "1015"
        assert provider.calls == [False, True]  # first normal, second forced
        # First request used tok-1; second used tok-2.
        assert route.calls[0].request.headers["Authorization"] == "Bearer tok-1"
        assert route.calls[1].request.headers["Authorization"] == "Bearer tok-2"

    @respx.mock
    async def test_gives_up_after_second_401(self) -> None:
        respx.get("http://oe.test/fhir/Patient/1015").mock(
            side_effect=[Response(401), Response(401)]
        )
        async with FhirClient("http://oe.test/fhir", StaticTokenProvider(token=_tok())) as client:
            with pytest.raises(FhirClientError):
                await client.read(ResourceType.Patient, "1015")
