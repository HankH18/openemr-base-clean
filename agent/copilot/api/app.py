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

import importlib
import pkgutil
from collections.abc import Awaitable, Callable
from functools import partial

from fastapi import APIRouter, FastAPI, Response
from fastapi.responses import JSONResponse

from copilot import __version__
from copilot.api import readiness, routes
from copilot.api.middleware import CorrelationIdMiddleware
from copilot.config import Settings, get_settings
from copilot.domain.contracts import HealthResponse, ReadinessDependency, ReadinessResponse
from copilot.memory.db import get_engine
from copilot.observability import build_observability

ProbeFactory = Callable[[Settings], Callable[[], Awaitable[ReadinessDependency]]]


def register_routers(app: FastAPI) -> None:
    """Auto-discover and mount feature routers.

    Every module under ``copilot.api.routes`` is imported; any that exposes a
    module-level ``router`` (a FastAPI ``APIRouter``) is mounted via
    ``include_router``. Modules with no ``router`` attribute are skipped;
    genuine import errors propagate rather than being swallowed, so a broken
    route module surfaces loudly at startup instead of silently vanishing.
    """
    for module_info in pkgutil.iter_modules(routes.__path__):
        module = importlib.import_module(f"{routes.__name__}.{module_info.name}")
        router = getattr(module, "router", None)
        if isinstance(router, APIRouter):
            app.include_router(router)


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
    # Distinguish "not supplied" (None -> wire real probes) from an explicit
    # empty list (caller wants no probes, e.g. tests). `or` would coerce [] to
    # the defaults; `is None` preserves the caller's empty list.
    if probe_factories is None:
        probe_factories = _default_probe_factories()

    app = FastAPI(
        title="Clinical Co-Pilot",
        version=__version__,
        # OpenAPI is the "runnable API collection" ARCHITECTURE.md calls for.
        docs_url="/docs",
        openapi_url="/openapi.json",
    )

    # Observability backend (Langfuse when creds present, else no-op) lives on
    # app.state so request handlers and background tasks share one instance.
    app.state.observability = build_observability(settings)

    # Every request gets a correlation ID published to the ContextVar and
    # echoed on the X-Correlation-ID response header.
    app.add_middleware(CorrelationIdMiddleware)

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

    # Feature routes (rounds, chat, …) mount themselves without edits here.
    register_routers(app)

    return app


# For `uvicorn copilot.api.app:app`.
app = create_app()
