"""Dependency probes used by `/ready`.

Each probe is small, isolated, and returns a `ReadinessDependency`.  The
readiness endpoint composes them.  Kept out of `app.py` so unit tests can
inject fakes without spinning up the full FastAPI app.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from copilot.config import Settings
from copilot.domain.contracts import ReadinessDependency


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


async def probe_llm(settings: Settings) -> ReadinessDependency:
    """LLM readiness — presence of API key.

    A real ping against Anthropic happens once the client is wired (Unit
    3).  For scaffold, absence of the key is not-ready by design —
    ``/ready`` refuses to say "yes" until the operator has provisioned it.
    """
    if settings.anthropic_api_key:
        return ReadinessDependency(name="llm", ok=True, detail="key present")
    return ReadinessDependency(name="llm", ok=False, detail="ANTHROPIC_API_KEY not set")


async def probe_langfuse(settings: Settings) -> ReadinessDependency:
    """Langfuse readiness — same posture as LLM.

    Observability is a hard dependency for the production posture in
    ARCHITECTURE.md; refuse ready until it's configured.
    """
    if settings.langfuse_host and settings.langfuse_public_key and settings.langfuse_secret_key:
        return ReadinessDependency(name="langfuse", ok=True, detail="creds present")
    return ReadinessDependency(name="langfuse", ok=False, detail="langfuse credentials not set")


async def run_all(
    probes: list[Callable[[], Awaitable[ReadinessDependency]]],
) -> list[ReadinessDependency]:
    """Run probes sequentially — order preserved for stable JSON output.

    Kept sequential rather than gathered so a slow probe doesn't blur
    which dependency caused the delay in traces.
    """
    return [await p() for p in probes]
