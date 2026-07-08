"""FastAPI application factory.

Constructed via `create_app()` so tests can inject fake readiness probes
without needing a live Postgres / OpenEMR.

Route surface implemented so far:

- ``GET /health`` — process liveness (200 as long as we can serve).
- ``GET /ready``  — depends on Postgres + OpenEMR FHIR + LLM + Langfuse.
  Returns 503 with the failing dependencies enumerated.

Chat + rounds endpoints are stubbed out later; scaffold ships routing
skeleton only for now (Unit 1 acceptance).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from functools import partial

from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse

from copilot import __version__
from copilot.api import readiness
from copilot.config import Settings, get_settings
from copilot.domain.contracts import HealthResponse, ReadinessDependency, ReadinessResponse
from copilot.memory.db import get_engine


ProbeFactory = Callable[[Settings], Callable[[], Awaitable[ReadinessDependency]]]


def _default_probe_factories() -> list[ProbeFactory]:
    """Wire up the real probes against real dependencies."""
    return [
        lambda s: partial(readiness.probe_postgres, get_engine()),
        lambda s: partial(readiness.probe_openemr_fhir, s),
        lambda s: partial(readiness.probe_llm, s),
        lambda s: partial(readiness.probe_langfuse, s),
    ]


def create_app(
    settings: Settings | None = None,
    probe_factories: list[ProbeFactory] | None = None,
) -> FastAPI:
    """Build the app.  All I/O collaborators are injectable."""
    settings = settings or get_settings()
    probe_factories = probe_factories or _default_probe_factories()

    app = FastAPI(
        title="Clinical Co-Pilot",
        version=__version__,
        # OpenAPI is the "runnable API collection" ARCHITECTURE.md calls for.
        docs_url="/docs",
        openapi_url="/openapi.json",
    )

    @app.get(
        "/health",
        response_model=HealthResponse,
        summary="Liveness — process is running",
    )
    async def health() -> HealthResponse:
        return HealthResponse(version=__version__)

    @app.get(
        "/ready",
        response_model=ReadinessResponse,
        summary="Readiness — all critical dependencies reachable",
        responses={200: {"description": "ready"}, 503: {"description": "not ready"}},
    )
    async def ready() -> Response:
        probes = [factory(settings) for factory in probe_factories]
        deps = await readiness.run_all(probes)
        payload = ReadinessResponse(ready=all(d.ok for d in deps), dependencies=deps)
        return JSONResponse(
            status_code=payload.to_status_code(),
            content=payload.model_dump(mode="json"),
        )

    return app


# For `uvicorn copilot.api.app:app`.
app = create_app()
