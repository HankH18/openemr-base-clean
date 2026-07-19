"""The PHI-free access record must log the route TEMPLATE, not the concrete path.

Guarded because the omission leaked identifiers into the record billed as the
PHI-free access trail. The middleware docstring promises "only non-PHI request
metadata (method, path template, status, latency)" and it scrubs exception
messages precisely because they "can carry patient data" — yet it recorded
``request.url.path``, the CONCRETE path. For path-param routes that concrete
path embeds resource ids: ``/v1/patients/{patient_id}/observations`` becomes
``/v1/patients/98765/observations``, so patient / document / conversation ids
land in the access log (and trip the project's graded ``no_phi_in_logs``
rubric).

Fix: after routing has resolved the request, log ``scope["route"].path`` (the
template ``/v1/documents/{document_id}``), falling back to the concrete
``url.path`` only when no route matched (a 404 to an unrouted path — which
carries no id anyway).
"""

from __future__ import annotations

import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from copilot.api.middleware import CorrelationIdMiddleware


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(CorrelationIdMiddleware)

    @app.get("/v1/documents/{document_id}")
    async def get_document(document_id: str) -> dict[str, str]:
        return {"id": document_id}

    @app.get("/v1/patients/{patient_id}/observations")
    async def boom(patient_id: str) -> dict[str, str]:
        raise RuntimeError(f"patient {patient_id} MRN 12345678 exploded")

    return app


def _access_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.message == "http.request"]


def test_success_record_logs_template_not_concrete_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = TestClient(_app())
    with caplog.at_level(logging.INFO, logger="copilot.api.access"):
        client.get("/v1/documents/123")
    rec = _access_records(caplog)[-1]
    # The template, with the id parameter left symbolic.
    assert rec.http_path == "/v1/documents/{document_id}"  # type: ignore[attr-defined]
    # The concrete id must NOT appear anywhere in the record.
    assert "123" not in rec.http_path  # type: ignore[attr-defined]


def test_failure_record_logs_template_not_concrete_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = TestClient(_app(), raise_server_exceptions=False)
    with caplog.at_level(logging.INFO, logger="copilot.api.access"):
        client.get("/v1/patients/98765/observations")
    rec = _access_records(caplog)[-1]
    assert rec.http_status == 500  # type: ignore[attr-defined]
    assert rec.http_path == "/v1/patients/{patient_id}/observations"  # type: ignore[attr-defined]
    # Neither the path param nor the MRN embedded in the exception may leak.
    assert "98765" not in rec.http_path  # type: ignore[attr-defined]
    blob = str(rec.__dict__)
    for leak in ("98765", "12345678", "exploded"):
        assert leak not in blob, f"the PHI-free access trail leaked {leak!r}"


def test_unrouted_path_falls_back_to_url_path(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A 404 to a path with no matching route has no resolved template; the
    # concrete url.path is fine here because there is no id to leak.
    client = TestClient(_app())
    with caplog.at_level(logging.INFO, logger="copilot.api.access"):
        client.get("/v1/no-such-route")
    rec = _access_records(caplog)[-1]
    assert rec.http_status == 404  # type: ignore[attr-defined]
    assert rec.http_path == "/v1/no-such-route"  # type: ignore[attr-defined]
