"""Tests for correlation-ID middleware, router auto-registration, and the
observability wiring added to ``create_app``.

The existing ``/health`` + ``/ready`` behavior must stay intact — those cases
live in ``test_health_ready.py``; here we assert only the additive plumbing.
"""

from __future__ import annotations

import re

from fastapi import FastAPI
from fastapi.testclient import TestClient

from copilot.api.app import create_app, register_routers
from copilot.api.middleware import (
    CORRELATION_ID_HEADER,
    CorrelationIdMiddleware,
    resolve_correlation_id,
)
from copilot.config import Settings
from copilot.observability import NoopObservability, current_correlation_id

_CORRELATION_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{8,64}$")


def _client() -> TestClient:
    return TestClient(create_app(settings=Settings(), probe_factories=[]))


# --- create_app wiring ------------------------------------------------------


def test_observability_is_stored_on_app_state() -> None:
    app = create_app(settings=Settings(), probe_factories=[])
    # No Langfuse creds in a bare Settings() -> the no-op backend.
    assert isinstance(app.state.observability, NoopObservability)


def test_health_still_returns_200() -> None:
    resp = _client().get("/health")
    assert resp.status_code == 200
    assert resp.json()["alive"] is True


def test_ready_returns_200_with_no_probes() -> None:
    resp = _client().get("/ready")
    assert resp.status_code == 200
    assert resp.json()["ready"] is True


# --- correlation-id response header -----------------------------------------


def test_health_response_carries_a_valid_correlation_id() -> None:
    resp = _client().get("/health")
    corr = resp.headers.get(CORRELATION_ID_HEADER)
    assert corr is not None
    assert _CORRELATION_ID_RE.match(corr)


def test_valid_incoming_correlation_id_is_echoed_back() -> None:
    provided = "client-supplied-id-0001"
    resp = _client().get("/health", headers={CORRELATION_ID_HEADER: provided})
    assert resp.headers.get(CORRELATION_ID_HEADER) == provided


def test_invalid_incoming_correlation_id_is_replaced_not_rejected() -> None:
    # Too short + illegal characters — must NOT 400; a fresh id is minted.
    resp = _client().get("/health", headers={CORRELATION_ID_HEADER: "bad id!"})
    assert resp.status_code == 200
    corr = resp.headers.get(CORRELATION_ID_HEADER)
    assert corr is not None
    assert corr != "bad id!"
    assert _CORRELATION_ID_RE.match(corr)


def test_each_request_gets_its_own_generated_id() -> None:
    client = _client()
    first = client.get("/health").headers[CORRELATION_ID_HEADER]
    second = client.get("/health").headers[CORRELATION_ID_HEADER]
    assert first != second


# --- resolve_correlation_id (unit) ------------------------------------------


def test_resolve_uses_valid_incoming_value_verbatim() -> None:
    assert resolve_correlation_id("valid-corr-id-42") == "valid-corr-id-42"


def test_resolve_strips_surrounding_whitespace() -> None:
    assert resolve_correlation_id("  valid-corr-id-42  ") == "valid-corr-id-42"


def test_resolve_generates_when_missing() -> None:
    generated = resolve_correlation_id(None)
    assert _CORRELATION_ID_RE.match(generated)


def test_resolve_generates_when_too_short() -> None:
    generated = resolve_correlation_id("short")
    assert generated != "short"
    assert _CORRELATION_ID_RE.match(generated)


def test_resolve_generates_when_illegal_characters() -> None:
    generated = resolve_correlation_id("has spaces and !!!")
    assert _CORRELATION_ID_RE.match(generated)


# --- ContextVar propagation into the request handler ------------------------


def test_context_var_is_set_for_the_duration_of_the_request() -> None:
    app = FastAPI()
    app.add_middleware(CorrelationIdMiddleware)

    @app.get("/echo")
    async def echo() -> dict[str, str]:
        return {"seen": current_correlation_id()}

    client = TestClient(app)
    provided = "ctx-var-echo-id-01"
    body = client.get("/echo", headers={CORRELATION_ID_HEADER: provided}).json()
    assert body["seen"] == provided


def test_context_var_resets_after_the_request() -> None:
    # Outside any request the ContextVar default ('') must be intact.
    _client().get("/health")
    assert current_correlation_id() == ""


# --- router auto-registration -----------------------------------------------


def test_register_routers_adds_nothing_when_package_is_empty() -> None:
    """No route modules exist yet, so only /health, /ready (+ docs) are exposed."""
    app = create_app(settings=Settings(), probe_factories=[])
    paths = {route.path for route in app.routes}  # type: ignore[attr-defined]
    assert "/health" in paths
    assert "/ready" in paths


def test_register_routers_is_idempotent_and_safe_to_call() -> None:
    app = create_app(settings=Settings(), probe_factories=[])
    before = len(app.routes)
    register_routers(app)  # no route modules -> no change
    assert len(app.routes) == before
