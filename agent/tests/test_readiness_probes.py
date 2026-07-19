"""Direct tests of individual probes (unit-level)."""

from __future__ import annotations

from functools import partial

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine

from copilot.api import readiness
from copilot.api.app import create_app
from copilot.config import Settings
from copilot.domain.contracts import ReadinessDependency


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


# --- embedder (Voyage): reachability, advisory ------------------------------


class _BoomClient(httpx.AsyncClient):
    """A client whose every outbound verb raises a transport error.

    Used to prove a KEYED-but-unreachable probe reports ``degraded`` rather than
    silently reporting ``ok`` on nothing but config presence.
    """

    async def get(self, *_args, **_kwargs):  # type: ignore[override]
        raise httpx.ConnectError("boom")

    async def post(self, *_args, **_kwargs):  # type: ignore[override]
        raise httpx.ConnectError("boom")


@pytest.mark.asyncio
async def test_probe_embedder_keyless_is_ok_stub_and_makes_no_network_call() -> None:
    """No key => stub reported ok/advisory, and NOT a single network call.

    The deployed config is keyless, so this path must never touch the network.
    The injected factory raises if anyone opens a client — proving it isn't.
    """

    def _forbidden_factory() -> httpx.AsyncClient:
        raise AssertionError("keyless embedder probe must not open an HTTP client")

    dep = await readiness.probe_embedder(
        Settings(voyage_api_key=""), client_factory=_forbidden_factory
    )
    assert dep.ok is True
    assert dep.advisory is True
    assert dep.detail == "stub (keyless)"
    assert dep.status == "ok"


@pytest.mark.asyncio
async def test_probe_embedder_keyed_and_reachable_is_ok() -> None:
    """A set key plus a provider that answers 200 is ready (reachable)."""
    settings = Settings(voyage_api_key="vk-testing", voyage_embedding_model="voyage-3.5")
    with respx.mock(base_url="https://api.voyageai.com") as mock:
        route = mock.post("/v1/embeddings").respond(
            200, json={"data": [{"index": 0, "embedding": [0.0]}]}
        )
        dep = await readiness.probe_embedder(settings)
    assert route.called, "keyed embedder probe must actually reach out (reachability, not presence)"
    assert dep.ok is True
    assert dep.status == "ok"
    assert "voyage-3.5" in dep.detail


@pytest.mark.asyncio
async def test_probe_embedder_keyed_but_unreachable_is_degraded_not_ok() -> None:
    """THE BITE: a key set but the backend unreachable must NOT report ok.

    Before the fix ``probe_embedder`` returned ``ok`` on config presence alone;
    an injected client that raises on the wire now surfaces as ``degraded``.
    """
    settings = Settings(voyage_api_key="vk-testing", voyage_embedding_model="voyage-3.5")
    dep = await readiness.probe_embedder(settings, client_factory=lambda: _BoomClient())
    assert dep.ok is False, "keyed-but-unreachable embedder must not silently report ok"
    assert dep.advisory is True, "advisory => it can be degraded but never 503s /ready"
    assert dep.status == "degraded", "non-gating: degraded, not down"
    assert "ConnectError" in dep.detail


@pytest.mark.asyncio
async def test_probe_embedder_keyed_but_5xx_is_degraded() -> None:
    """A reachable-but-erroring endpoint (500) is degraded, not ok."""
    settings = Settings(voyage_api_key="vk-testing", voyage_embedding_model="voyage-3.5")
    with respx.mock(base_url="https://api.voyageai.com") as mock:
        mock.post("/v1/embeddings").respond(500)
        dep = await readiness.probe_embedder(settings)
    assert dep.ok is False
    assert dep.status == "degraded"
    assert "500" in dep.detail


# --- reranker (Cohere): reachability, advisory ------------------------------


@pytest.mark.asyncio
async def test_probe_reranker_keyless_is_ok_stub_and_makes_no_network_call() -> None:
    """No key => stub ok/advisory with zero network calls (deployed config)."""

    def _forbidden_factory() -> httpx.AsyncClient:
        raise AssertionError("keyless reranker probe must not open an HTTP client")

    dep = await readiness.probe_reranker(
        Settings(cohere_api_key=""), client_factory=_forbidden_factory
    )
    assert dep.ok is True
    assert dep.advisory is True
    assert dep.detail == "stub (keyless)"
    assert dep.status == "ok"


@pytest.mark.asyncio
async def test_probe_reranker_keyed_and_reachable_is_ok() -> None:
    """A set key plus a Cohere models-list that answers 200 is ready."""
    settings = Settings(cohere_api_key="ck-testing", cohere_rerank_model="rerank-v3.5")
    with respx.mock(base_url="https://api.cohere.com") as mock:
        route = mock.get("/v1/models").respond(200, json={"models": []})
        dep = await readiness.probe_reranker(settings)
    assert route.called, "keyed reranker probe must actually reach out (reachability, not presence)"
    assert dep.ok is True
    assert dep.status == "ok"
    assert "rerank-v3.5" in dep.detail


@pytest.mark.asyncio
async def test_probe_reranker_keyed_but_unreachable_is_degraded_not_ok() -> None:
    """THE BITE: keyed but Cohere unreachable must report degraded, not ok."""
    settings = Settings(cohere_api_key="ck-testing", cohere_rerank_model="rerank-v3.5")
    dep = await readiness.probe_reranker(settings, client_factory=lambda: _BoomClient())
    assert dep.ok is False, "keyed-but-unreachable reranker must not silently report ok"
    assert dep.advisory is True
    assert dep.status == "degraded"
    assert "ConnectError" in dep.detail


@pytest.mark.asyncio
async def test_probe_reranker_keyed_but_5xx_is_degraded() -> None:
    """A reachable-but-erroring endpoint (503) is degraded, not ok."""
    settings = Settings(cohere_api_key="ck-testing", cohere_rerank_model="rerank-v3.5")
    with respx.mock(base_url="https://api.cohere.com") as mock:
        mock.get("/v1/models").respond(503)
        dep = await readiness.probe_reranker(settings)
    assert dep.ok is False
    assert dep.status == "degraded"
    assert "503" in dep.detail


def test_keyed_unreachable_rerank_and_embed_never_503_ready() -> None:
    """End-to-end: keyed-but-unreachable embed/rerank degrade /ready, never 503 it.

    Proves the full chain — a degraded advisory probe stays out of the readiness
    conjunction, so ``/ready`` remains 200 while honestly reporting the degrade.
    """

    async def _ok_gating() -> ReadinessDependency:
        return ReadinessDependency(name="document_store", ok=True, detail="reachable")

    settings = Settings(voyage_api_key="vk", cohere_api_key="ck")
    factories = [
        lambda _s: _ok_gating,
        lambda s: partial(readiness.probe_embedder, s, lambda: _BoomClient()),
        lambda s: partial(readiness.probe_reranker, s, lambda: _BoomClient()),
    ]
    client = TestClient(create_app(settings=settings, probe_factories=factories))
    resp = client.get("/ready")
    assert resp.status_code == 200, "advisory embed/rerank degrade must NOT pull /ready out of rotation"
    grades = {d["name"]: d["status"] for d in resp.json()["dependencies"]}
    assert grades["embedder"] == "degraded"
    assert grades["reranker"] == "degraded"
