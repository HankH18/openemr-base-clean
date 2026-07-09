"""Tests for FHIR ``search`` Bundle pagination (following ``link[next]``)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import respx
from httpx import Response

from copilot.domain.primitives import ResourceType
from copilot.fhir.auth import OAuthToken, StaticTokenProvider
from copilot.fhir.client import FhirClient

pytestmark = pytest.mark.asyncio


def _tok(value: str = "abc") -> OAuthToken:
    return OAuthToken(
        access_token=value,
        token_type="Bearer",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


def _entry(resource_id: str) -> dict[str, Any]:
    return {"resource": {"resourceType": "Observation", "id": resource_id}}


def _page(entries: list[dict[str, Any]], *, next_url: str | None) -> dict[str, Any]:
    links: list[dict[str, str]] = [{"relation": "self", "url": "http://oe.test/self"}]
    if next_url is not None:
        links.append({"relation": "next", "url": next_url})
    return {
        "resourceType": "Bundle",
        "type": "searchset",
        "total": 99,  # server-reported match count; aggregate must override it
        "link": links,
        "entry": entries,
    }


class TestSinglePage:
    @respx.mock
    async def test_bundle_without_next_link_is_returned_untouched(self) -> None:
        original = {
            "resourceType": "Bundle",
            "type": "searchset",
            "total": 3,
            "entry": [_entry("1"), _entry("2"), _entry("3")],
        }
        respx.get("http://oe.test/fhir/Observation").mock(return_value=Response(200, json=original))
        async with FhirClient("http://oe.test/fhir", StaticTokenProvider(token=_tok())) as client:
            body = await client.search(ResourceType.Observation, {"patient": "1015"})
        # Byte-for-byte identical to today's single-fetch behaviour.
        assert body == original
        assert body["total"] == 3
        assert len(body["entry"]) == 3

    @respx.mock
    async def test_self_only_link_list_is_not_paginated(self) -> None:
        original = _page([_entry("1")], next_url=None)  # only a self link
        route = respx.get("http://oe.test/fhir/Observation").mock(
            return_value=Response(200, json=original)
        )
        async with FhirClient("http://oe.test/fhir", StaticTokenProvider(token=_tok())) as client:
            body = await client.search(ResourceType.Observation, {"patient": "1015"})
        assert body == original  # unchanged: total stays server-reported 99
        assert len(route.calls) == 1


class TestMultiPage:
    @respx.mock
    async def test_follows_next_chain_and_aggregates_entries(self) -> None:
        p2 = "http://oe.test/fhir/Observation?_page=2"
        p3 = "http://oe.test/fhir/Observation?_page=3"
        respx.get("http://oe.test/fhir/Observation", params={"patient": "1015"}).mock(
            return_value=Response(200, json=_page([_entry("1"), _entry("2")], next_url=p2))
        )
        route2 = respx.get(p2).mock(
            return_value=Response(200, json=_page([_entry("3"), _entry("4")], next_url=p3))
        )
        route3 = respx.get(p3).mock(
            return_value=Response(200, json=_page([_entry("5")], next_url=None))
        )

        async with FhirClient("http://oe.test/fhir", StaticTokenProvider(token=_tok())) as client:
            body = await client.search(ResourceType.Observation, {"patient": "1015"})

        ids = [e["resource"]["id"] for e in body["entry"]]
        assert ids == ["1", "2", "3", "4", "5"]
        # total reflects the count actually returned, not the server's 99.
        assert body["total"] == 5
        assert body["resourceType"] == "Bundle"
        assert route2.called and route3.called

    @respx.mock
    async def test_each_page_fetch_carries_bearer_token(self) -> None:
        p2 = "http://oe.test/fhir/Observation?_page=2"
        respx.get("http://oe.test/fhir/Observation", params={"patient": "1015"}).mock(
            return_value=Response(200, json=_page([_entry("1")], next_url=p2))
        )
        route2 = respx.get(p2).mock(
            return_value=Response(200, json=_page([_entry("2")], next_url=None))
        )
        async with FhirClient(
            "http://oe.test/fhir", StaticTokenProvider(token=_tok("page-jwt"))
        ) as client:
            body = await client.search(ResourceType.Observation, {"patient": "1015"})
        assert len(body["entry"]) == 2
        assert route2.calls[0].request.headers["Authorization"] == "Bearer page-jwt"
        assert "fhir+json" in route2.calls[0].request.headers["Accept"]


class TestLoopGuards:
    @respx.mock
    async def test_cyclic_next_link_terminates(self) -> None:
        p2 = "http://oe.test/fhir/Observation?_page=2"
        respx.get("http://oe.test/fhir/Observation", params={"patient": "1015"}).mock(
            return_value=Response(200, json=_page([_entry("1")], next_url=p2))
        )
        # Page 2 points back at itself — must stop, not spin forever.
        route2 = respx.get(p2).mock(
            return_value=Response(200, json=_page([_entry("2")], next_url=p2))
        )
        async with FhirClient("http://oe.test/fhir", StaticTokenProvider(token=_tok())) as client:
            body = await client.search(ResourceType.Observation, {"patient": "1015"})
        ids = [e["resource"]["id"] for e in body["entry"]]
        assert ids == ["1", "2"]
        assert len(route2.calls) == 1  # fetched exactly once despite self-cycle
