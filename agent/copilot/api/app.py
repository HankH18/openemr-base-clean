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
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
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
from copilot.observability.logging import configure_logging

ProbeFactory = Callable[[Settings], Callable[[], Awaitable[ReadinessDependency]]]


def register_routers(app: FastAPI) -> None:
    """Auto-discover and mount feature routers.

    Every module under ``copilot.api.routes`` is imported; any that exposes a
    module-level ``router`` (a FastAPI ``APIRouter``) is mounted via
    ``include_router``. Modules with no ``router`` attribute are skipped;
    genuine import errors propagate rather than being swallowed, so a broken
    route module surfaces loudly at startup instead of silently vanishing.

    Idempotent: a router module already mounted on this app is skipped on any
    repeat call, so calling this twice never double-registers routes.
    """
    registered: set[str] = getattr(app.state, "_registered_route_modules", set())
    for module_info in pkgutil.iter_modules(routes.__path__):
        if module_info.name in registered:
            continue
        module = importlib.import_module(f"{routes.__name__}.{module_info.name}")
        router = getattr(module, "router", None)
        if isinstance(router, APIRouter):
            app.include_router(router)
            registered.add(module_info.name)
    app.state._registered_route_modules = registered


def _default_probe_factories() -> list[ProbeFactory]:
    """Wire up the real probes against real dependencies.

    Week-2 graded readiness surfaces the ingestion + RAG dependencies
    (``document_store``, ``pgvector``, ``guideline_corpus``, ``embedder``,
    ``reranker``) alongside the Week-1 external dependencies (OpenEMR FHIR, LLM,
    Langfuse). The agent document store replaces the bare ``postgres`` probe — it
    is the same connection, named for what it backs. ``guideline_corpus`` grades
    the *content* of that store rather than its reachability: a deploy that skips
    the manual corpus ingest is degraded-but-serving, not ready.
    """
    return [
        lambda s: partial(readiness.probe_document_store, get_engine()),
        lambda s: partial(readiness.probe_pgvector, s, get_engine()),
        lambda s: partial(readiness.probe_guideline_corpus, get_engine()),
        lambda s: partial(readiness.probe_embedder, s),
        lambda s: partial(readiness.probe_reranker, s),
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
    # Structured JSON logging, correlation-id-tagged, on stdout. Idempotent, so
    # rebuilding the app (tests, workers) never stacks handlers.
    configure_logging()
    # Distinguish "not supplied" (None -> wire real probes) from an explicit
    # empty list (caller wants no probes, e.g. tests). `or` would coerce [] to
    # the defaults; `is None` preserves the caller's empty list.
    if probe_factories is None:
        probe_factories = _default_probe_factories()

    @asynccontextmanager
    async def lifespan(app_: FastAPI) -> AsyncIterator[None]:
        """Run the background poller (when enabled) and flush observability.

        The poller is gated OFF by default: when ``settings.poller_enabled`` is
        false nothing is imported and no scheduler starts, and the app boots
        exactly as before with no dependency on Postgres/OpenEMR. Independently
        of the poller, buffered observability events are flushed on shutdown so
        a Langfuse backend delivers everything before the process exits.
        """
        try:
            if settings.poller_enabled:
                # Imported lazily so the poller's collaborators are never
                # loaded when the feature is off.
                from copilot.worker.runtime import build_poller_scheduler

                scheduler = build_poller_scheduler(settings, app_.state.observability)
                scheduler.start()
                try:
                    yield
                finally:
                    scheduler.shutdown()
            else:
                yield
        finally:
            await app_.state.observability.flush()

    app = FastAPI(
        title="Clinical Co-Pilot",
        version=__version__,
        # OpenAPI is the "runnable API collection" ARCHITECTURE.md calls for.
        docs_url="/docs",
        openapi_url="/openapi.json",
        lifespan=lifespan,
    )

    # Observability backend (Langfuse when creds present, else no-op) lives on
    # app.state so request handlers and background tasks share one instance.
    app.state.observability = build_observability(settings)

    # Every request gets a correlation ID published to the ContextVar and
    # echoed on the X-Correlation-ID response header.
    app.add_middleware(CorrelationIdMiddleware)

    # Optional CORS — for a split-origin UI or a local browser demo. Off unless
    # origins are configured; the same-origin (reverse-proxy) deploy needs none.
    if settings.cors_allow_origins:
        from fastapi.middleware.cors import CORSMiddleware

        app.add_middleware(
            CORSMiddleware,
            allow_origins=[o.strip() for o in settings.cors_allow_origins.split(",") if o.strip()],
            allow_methods=["*"],
            allow_headers=["*"],
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
        payload = ReadinessResponse.from_dependencies(deps)
        return JSONResponse(
            status_code=payload.to_status_code(),
            content=payload.model_dump(mode="json"),
        )

    # Feature routes (rounds, chat, …) mount themselves without edits here.
    register_routers(app)

    return app


# For `uvicorn copilot.api.app:app`.
app = create_app()
