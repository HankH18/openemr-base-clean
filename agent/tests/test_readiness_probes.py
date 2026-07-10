"""Direct tests of individual probes (unit-level)."""

from __future__ import annotations

import httpx
import pytest
import respx
from sqlalchemy.ext.asyncio import create_async_engine

from copilot.api import readiness
from copilot.config import Settings


@pytest.mark.asyncio
async def test_probe_postgres_ok_against_sqlite_memory() -> None:
    """An in-memory aiosqlite engine should always answer SELECT 1."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        dep = await readiness.probe_postgres(engine)
        assert dep.ok is True
        assert dep.name == "postgres"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_probe_postgres_fail_on_bad_url() -> None:
    engine = create_async_engine("postgresql+psycopg://user:pw@127.0.0.1:1/nope")
    try:
        dep = await readiness.probe_postgres(engine)
        assert dep.ok is False
        assert dep.detail  # some error type name
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_probe_openemr_fhir_ok_when_capability_returned() -> None:
    settings = Settings(fhir_base_url="http://openemr.test/apis/default/fhir")
    with respx.mock(base_url="http://openemr.test") as mock:
        mock.get("/apis/default/fhir/metadata").respond(
            200, json={"resourceType": "CapabilityStatement", "status": "active"}
        )
        dep = await readiness.probe_openemr_fhir(settings)
    assert dep.ok is True


@pytest.mark.asyncio
async def test_probe_openemr_fhir_fail_on_non_200() -> None:
    settings = Settings(fhir_base_url="http://openemr.test/apis/default/fhir")
    with respx.mock(base_url="http://openemr.test") as mock:
        mock.get("/apis/default/fhir/metadata").respond(500)
        dep = await readiness.probe_openemr_fhir(settings)
    assert dep.ok is False
    assert "500" in dep.detail


@pytest.mark.asyncio
async def test_probe_openemr_fhir_fail_on_connection_error() -> None:
    """Route through a factory that raises to simulate DNS/connect failure."""
    settings = Settings(fhir_base_url="http://openemr.test/apis/default/fhir")

    class _BoomClient(httpx.AsyncClient):
        async def get(self, *_args, **_kwargs):  # type: ignore[override]
            raise httpx.ConnectError("boom")

    dep = await readiness.probe_openemr_fhir(settings, client_factory=lambda: _BoomClient())
    assert dep.ok is False
    assert dep.detail == "ConnectError"


@pytest.mark.asyncio
async def test_probe_llm_ok_when_backend_reachable() -> None:
    """A set key plus a reachable provider (any HTTP response) is ready."""
    settings = Settings(anthropic_api_key="sk-testing", anthropic_base_url="https://llm.test")
    with respx.mock(base_url="https://llm.test") as mock:
        mock.get("/v1/models").respond(200, json={"data": []})
        dep = await readiness.probe_llm(settings)
    assert dep.ok is True
    assert dep.name == "llm"


@pytest.mark.asyncio
async def test_probe_llm_not_ready_when_key_missing() -> None:
    dep = await readiness.probe_llm(Settings(anthropic_api_key=""))
    assert dep.ok is False
    assert "ANTHROPIC_API_KEY" in dep.detail


@pytest.mark.asyncio
async def test_probe_llm_fail_when_backend_unreachable() -> None:
    """A set key pointed at a dead provider is NOT ready — reachability, not presence."""
    settings = Settings(anthropic_api_key="sk-testing", anthropic_base_url="https://llm.test")
    with respx.mock(base_url="https://llm.test") as mock:
        mock.get("/v1/models").mock(side_effect=httpx.ConnectError("boom"))
        dep = await readiness.probe_llm(settings)
    assert dep.ok is False
    assert dep.detail == "ConnectError"


@pytest.mark.asyncio
async def test_probe_langfuse_ok_when_host_reachable() -> None:
    settings = Settings(
        langfuse_host="https://langfuse.test",
        langfuse_public_key="pk",
        langfuse_secret_key="sk",
    )
    with respx.mock(base_url="https://langfuse.test") as mock:
        mock.get("/api/public/health").respond(200, json={"status": "OK"})
        dep = await readiness.probe_langfuse(settings)
    assert dep.ok is True
    assert dep.advisory is True  # observability is reported but never gates readiness


@pytest.mark.asyncio
async def test_probe_langfuse_not_ready_but_advisory_when_not_configured() -> None:
    partial = await readiness.probe_langfuse(
        Settings(langfuse_host="https://langfuse.test", langfuse_public_key="pk")
    )
    assert partial.ok is False
    assert partial.advisory is True


@pytest.mark.asyncio
async def test_probe_langfuse_fail_when_host_unreachable() -> None:
    """Creds set but the host is down: not reachable => not ok, still advisory."""
    settings = Settings(
        langfuse_host="https://langfuse.test",
        langfuse_public_key="pk",
        langfuse_secret_key="sk",
    )
    with respx.mock(base_url="https://langfuse.test") as mock:
        mock.get("/api/public/health").mock(side_effect=httpx.ConnectError("down"))
        dep = await readiness.probe_langfuse(settings)
    assert dep.ok is False
    assert dep.advisory is True
