"""Dependency probes used by `/ready`.

Each probe is small, isolated, and returns a `ReadinessDependency`.  The
readiness endpoint composes them.  Kept out of `app.py` so unit tests can
inject fakes without spinning up the full FastAPI app.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol

import httpx
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from copilot.config import Settings
from copilot.domain.contracts import ReadinessDependency
from copilot.memory.models import GuidelineChunkRow


class DependencyProbe(Protocol):
    """A callable that returns a ReadinessDependency, async."""

    async def __call__(self) -> ReadinessDependency: ...


async def probe_postgres(engine: AsyncEngine) -> ReadinessDependency:
    """`SELECT 1` — proves the URL works and the pool can hand out a conn."""
    try:
        async with engine.connect() as conn:
            result = await conn.execute(text("SELECT 1"))
            _ = result.scalar_one()
        return ReadinessDependency(name="postgres", ok=True)
    except Exception as exc:
        return ReadinessDependency(name="postgres", ok=False, detail=type(exc).__name__)


async def probe_document_store(engine: AsyncEngine) -> ReadinessDependency:
    """Agent document store — the Postgres that holds source_document / pages / facts.

    A ``SELECT 1`` proves the URL works and the pool can hand out a connection;
    the ingestion pipeline persists every derived artifact here, so it is a
    gating dependency (a down store means uploads cannot be recorded).
    """
    try:
        async with engine.connect() as conn:
            result = await conn.execute(text("SELECT 1"))
            _ = result.scalar_one()
        return ReadinessDependency(name="document_store", ok=True, detail="reachable")
    except Exception as exc:
        return ReadinessDependency(name="document_store", ok=False, detail=type(exc).__name__)


async def probe_pgvector(settings: Settings, engine: AsyncEngine) -> ReadinessDependency:
    """pgvector availability — dense guideline retrieval needs the vector extension.

    On Postgres the ``vector`` extension must be installed for ANN search; a
    missing extension is a real degradation (dense retrieval falls back to
    sparse-only), so it is reported advisory — sparse retrieval still serves. On
    SQLite (tests / keyless dev) the JSON fallback column is used, which is
    always available, so the probe reports ready.
    """
    if not settings.database_url.startswith("postgresql"):
        return ReadinessDependency(
            name="pgvector", ok=True, detail="sqlite json-vector fallback", advisory=True
        )
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                text("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
            )
            installed = result.first() is not None
        if installed:
            return ReadinessDependency(name="pgvector", ok=True, detail="extension installed")
        return ReadinessDependency(
            name="pgvector", ok=False, detail="vector extension not installed", advisory=True
        )
    except Exception as exc:
        return ReadinessDependency(
            name="pgvector", ok=False, detail=type(exc).__name__, advisory=True
        )


async def probe_guideline_corpus(engine: AsyncEngine) -> ReadinessDependency:
    """Guideline corpus population — an empty corpus serves zero evidence.

    The migrations create ``guideline_chunk`` empty; the corpus is loaded by a
    separate, manual step (``scripts/ingest_guidelines.py`` — DEPLOY.md §18
    step 4). Skipping it leaves hybrid retrieval structurally unable to return
    anything while every other dependency reports healthy, so the deployment
    looks ready and silently answers with no evidence.

    Advisory on every branch, like ``probe_langfuse``: an empty corpus is a real
    degradation but the service still serves (chat answers from FHIR facts; the
    retriever returns ``[]``, which is honest no-evidence rather than a
    fabricated cite), so it must never 503 the deployment out of rotation.
    """
    try:
        async with engine.connect() as conn:
            result = await conn.execute(select(func.count()).select_from(GuidelineChunkRow))
            count = int(result.scalar_one() or 0)
    except Exception as exc:
        return ReadinessDependency(
            name="guideline_corpus", ok=False, detail=type(exc).__name__, advisory=True
        )
    if count == 0:
        return ReadinessDependency(
            name="guideline_corpus",
            ok=False,
            detail=(
                "empty corpus — guideline retrieval returns no evidence; run "
                "scripts/ingest_guidelines.py (DEPLOY.md §18 step 4)"
            ),
            advisory=True,
        )
    return ReadinessDependency(
        name="guideline_corpus", ok=True, detail=f"{count} chunks", advisory=True
    )


async def probe_embedder(settings: Settings) -> ReadinessDependency:
    """Guideline embedder — Voyage when keyed, the deterministic stub otherwise.

    Keyless is a supported mode (the deterministic stub embeds offline), so the
    dependency is ready either way; the detail names which backend is active so a
    dashboard can see when it is running on the stub.
    """
    if settings.voyage_api_key:
        return ReadinessDependency(
            name="embedder", ok=True, detail=f"voyage:{settings.voyage_embedding_model}"
        )
    return ReadinessDependency(name="embedder", ok=True, detail="stub (keyless)", advisory=True)


async def probe_reranker(settings: Settings) -> ReadinessDependency:
    """Retrieval reranker — Cohere when keyed, the deterministic stub otherwise.

    Like the embedder, keyless is supported (fused sparse+dense order is served
    without a rerank), so this never gates readiness; the detail records the mode.
    """
    if settings.cohere_api_key:
        return ReadinessDependency(
            name="reranker", ok=True, detail=f"cohere:{settings.cohere_rerank_model}"
        )
    return ReadinessDependency(name="reranker", ok=True, detail="stub (keyless)", advisory=True)


async def probe_openemr_fhir(
    settings: Settings, client_factory: Callable[[], httpx.AsyncClient] | None = None
) -> ReadinessDependency:
    """`GET {fhir_base}/metadata` — CapabilityStatement is public, no auth needed."""
    factory = client_factory or (lambda: httpx.AsyncClient(timeout=5.0))
    url = settings.fhir_base_url.rstrip("/") + "/metadata"
    try:
        async with factory() as client:
            resp = await client.get(url)
        if resp.status_code == 200 and "CapabilityStatement" in resp.text[:200]:
            return ReadinessDependency(name="openemr_fhir", ok=True)
        return ReadinessDependency(
            name="openemr_fhir", ok=False, detail=f"status={resp.status_code}"
        )
    except Exception as exc:
        return ReadinessDependency(name="openemr_fhir", ok=False, detail=type(exc).__name__)


async def probe_llm(
    settings: Settings, client_factory: Callable[[], httpx.AsyncClient] | None = None
) -> ReadinessDependency:
    """LLM readiness — the provider must be *reachable*, not merely configured.

    A set key pointed at a dead backend is not ready: we attempt a short
    ``GET {anthropic_base_url}/v1/models`` with the key. Any HTTP response
    (even 401 for a bad key) proves the endpoint answered, so the provider is
    reachable; only a transport failure (refused/DNS/timeout) is not-ready.
    Absence of the key stays not-ready without a network call — there is
    nothing to ping.
    """
    if not settings.anthropic_api_key:
        return ReadinessDependency(name="llm", ok=False, detail="ANTHROPIC_API_KEY not set")
    factory = client_factory or (lambda: httpx.AsyncClient(timeout=2.0))
    url = settings.anthropic_base_url.rstrip("/") + "/v1/models"
    headers = {
        "x-api-key": settings.anthropic_api_key,
        "anthropic-version": "2023-06-01",
    }
    try:
        async with factory() as client:
            resp = await client.get(url, headers=headers)
        # Reachable == the real provider answered with a well-formed 200 payload.
        # A bare/empty 200 (a dead port, a catch-all proxy) is NOT proof the LLM
        # backend is up — mirror probe_openemr_fhir, which validates content, not
        # just status.
        if resp.status_code == 200 and isinstance(resp.json(), dict):
            return ReadinessDependency(name="llm", ok=True, detail="reachable")
        return ReadinessDependency(name="llm", ok=False, detail=f"status={resp.status_code}")
    except Exception as exc:
        return ReadinessDependency(name="llm", ok=False, detail=type(exc).__name__)


async def probe_langfuse(
    settings: Settings, client_factory: Callable[[], httpx.AsyncClient] | None = None
) -> ReadinessDependency:
    """Langfuse readiness — reachability, but advisory (never gating).

    Observability is valuable but must not pull the service out of rotation, so
    the result is always flagged ``advisory``: a failing Langfuse never turns
    ``/ready`` into a 503. Semantics still verify reachability rather than mere
    credential presence — with all three creds set we ping the host's public
    health endpoint (short timeout); a transport failure reports not-ok.
    """
    configured = bool(
        settings.langfuse_host and settings.langfuse_public_key and settings.langfuse_secret_key
    )
    if not configured:
        return ReadinessDependency(
            name="langfuse", ok=False, detail="not configured (advisory)", advisory=True
        )
    factory = client_factory or (lambda: httpx.AsyncClient(timeout=2.0))
    url = settings.langfuse_host.rstrip("/") + "/api/public/health"
    try:
        async with factory() as client:
            resp = await client.get(url)
        # As with the LLM probe, an empty/opaque 200 does not prove Langfuse is
        # actually up; require a well-formed JSON health payload.
        if resp.status_code == 200 and isinstance(resp.json(), dict):
            return ReadinessDependency(name="langfuse", ok=True, detail="reachable", advisory=True)
        return ReadinessDependency(
            name="langfuse", ok=False, detail=f"status={resp.status_code}", advisory=True
        )
    except Exception as exc:
        return ReadinessDependency(
            name="langfuse", ok=False, detail=type(exc).__name__, advisory=True
        )


async def run_all(
    probes: list[Callable[[], Awaitable[ReadinessDependency]]],
) -> list[ReadinessDependency]:
    """Run probes sequentially — order preserved for stable JSON output.

    Kept sequential rather than gathered so a slow probe doesn't blur
    which dependency caused the delay in traces.
    """
    return [await p() for p in probes]
