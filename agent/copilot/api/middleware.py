"""Request-scoped correlation-ID middleware.

Every request carries a correlation ID: read from an incoming
``X-Correlation-ID`` header when it satisfies the ``CorrelationId``
constraint, otherwise freshly generated. The ID is published to the
``copilot_correlation_id`` ContextVar for the lifetime of the request so
every downstream log line, LLM call, and verification step can pick it up
without threading it through call signatures, and it is echoed back on the
response ``X-Correlation-ID`` header for client-side correlation.

An invalid incoming ID is never rejected — we simply mint a fresh one, so a
misbehaving client can never turn a request into a 400 here.
"""

from __future__ import annotations

import logging
import time

from pydantic import TypeAdapter, ValidationError
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from copilot.domain.primitives import CorrelationId
from copilot.observability import correlation_id_var, generate_correlation_id

CORRELATION_ID_HEADER = "X-Correlation-ID"

# Access log — one structured JSON record per request. Emitted while the
# correlation id is still bound to the context, so the JSON logging filter
# stamps it with the same id echoed on the response header. Carries only
# non-PHI request metadata (method, path template, status, latency).
_access_logger = logging.getLogger("copilot.api.access")

# Reuse the domain constraint (8-64 chars, [A-Za-z0-9_-]) so the middleware
# and the rest of the system agree on what a valid correlation ID is.
_correlation_id_adapter: TypeAdapter[str] = TypeAdapter(CorrelationId)


def resolve_correlation_id(raw: str | None) -> str:
    """Return a valid correlation ID.

    The incoming value is used when it satisfies the ``CorrelationId``
    constraint; anything else (missing, malformed, wrong length) yields a
    freshly generated ID.
    """
    if raw is not None:
        try:
            return _correlation_id_adapter.validate_python(raw)
        except ValidationError:
            pass
    return generate_correlation_id()


def _access_log_path(request: Request) -> str:
    """Return the route TEMPLATE for the access record, never a concrete path.

    Once ``call_next`` has run, routing has resolved and the matched route is
    published on ``scope["route"]``; its ``.path`` is the template
    (``/v1/documents/{document_id}``), which keeps resource ids — patient,
    document, conversation — out of the record billed as the PHI-free access
    trail. Query strings are already excluded, and remain so.

    Falls back to the concrete ``request.url.path`` only when no route matched
    (e.g. a 404 to an unrouted path), which carries no path-param id to leak.
    """
    template = getattr(request.scope.get("route"), "path", None)
    if isinstance(template, str):
        return template
    return request.url.path


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """Publish a correlation ID for the request and echo it on the response."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        correlation_id = resolve_correlation_id(request.headers.get(CORRELATION_ID_HEADER))
        token = correlation_id_var.set(correlation_id)
        started = time.perf_counter()
        try:
            response = await call_next(request)
            # Log while the correlation id is still in context so the JSON filter
            # stamps this record with the request's id. Non-PHI metadata only.
            _access_logger.info(
                "http.request",
                extra={
                    "http_method": request.method,
                    "http_path": _access_log_path(request),
                    "http_status": response.status_code,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                },
            )
            response.headers[CORRELATION_ID_HEADER] = correlation_id
            return response
        except Exception:
            # An unhandled exception used to emit NOTHING: the success log was the
            # only one, so the very requests an error rate is computed FROM were
            # the ones missing from the log. A dashboard fed by these records
            # reported a healthy zero while the app was failing. Log the failure
            # (status 500 — what the server will actually return) with the same
            # shape and the correlation id still in context, then re-raise
            # untouched so the app's error handling is unchanged.
            #
            # No exception message or traceback: this record is the PHI-free
            # access trail, and an exception string can carry patient data. The
            # correlation id is the join key to the full trace.
            _access_logger.error(
                "http.request",
                extra={
                    "http_method": request.method,
                    "http_path": _access_log_path(request),
                    "http_status": 500,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                },
            )
            raise
        finally:
            correlation_id_var.reset(token)
