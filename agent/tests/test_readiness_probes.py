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
async def test_probe_llm_ok_when_key_present() -> None:
    dep = await readiness.probe_llm(Settings(anthropic_api_key="sk-testing"))
    assert dep.ok is True


@pytest.mark.asyncio
async def test_probe_llm_not_ready_when_key_missing() -> None:
    dep = await readiness.probe_llm(Settings(anthropic_api_key=""))
    assert dep.ok is False
    assert "ANTHROPIC_API_KEY" in dep.detail


@pytest.mark.asyncio
async def test_probe_langfuse_requires_all_three_env_vars() -> None:
    ok = await readiness.probe_langfuse(
        Settings(
            langfuse_host="https://cloud.langfuse.com",
            langfuse_public_key="pk",
            langfuse_secret_key="sk",
        )
    )
    assert ok.ok is True

    partial = await readiness.probe_langfuse(
        Settings(langfuse_host="https://cloud.langfuse.com", langfuse_public_key="pk")
    )
    assert partial.ok is False
