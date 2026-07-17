"""A failed request must still emit an access log.

Guarded because the omission was self-concealing: the access log was written only
on the success path, so an unhandled exception produced NO record at all. Every
request an error rate is computed FROM was precisely the one missing from the
log — a dashboard fed by these records reports a healthy zero while the app is
failing. Spec p6 requires request count AND error count.

Also pinned: the failure record carries no exception message or traceback. This
is the PHI-free access trail and an exception string can carry patient data; the
correlation id is the join key to the full trace.
"""

from __future__ import annotations

import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from copilot.api.middleware import CORRELATION_ID_HEADER, CorrelationIdMiddleware


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(CorrelationIdMiddleware)

    @app.get("/boom")
    async def boom() -> dict[str, str]:
        raise RuntimeError("patient Jane Doe MRN 12345678 exploded")

    @app.get("/fine")
    async def fine() -> dict[str, str]:
        return {"ok": "yes"}

    return app


def test_a_raising_request_still_logs_with_status_500(caplog: pytest.LogCaptureFixture) -> None:
    client = TestClient(_app(), raise_server_exceptions=False)
    with caplog.at_level(logging.INFO, logger="copilot.api.access"):
        client.get("/boom")
    records = [r for r in caplog.records if r.message == "http.request"]
    assert records, "a failed request emitted NO access log — the error is invisible"
    assert records[-1].http_status == 500  # type: ignore[attr-defined]
    assert records[-1].levelno == logging.ERROR


def test_the_failure_record_leaks_no_exception_detail(caplog: pytest.LogCaptureFixture) -> None:
    client = TestClient(_app(), raise_server_exceptions=False)
    with caplog.at_level(logging.INFO, logger="copilot.api.access"):
        client.get("/boom")
    blob = " ".join(str(getattr(r, "msg", "")) + str(r.__dict__) for r in caplog.records
                    if r.name == "copilot.api.access")
    for leak in ("Jane", "Doe", "12345678", "exploded"):
        assert leak not in blob, f"the PHI-free access trail leaked {leak!r}"


def test_the_exception_still_propagates() -> None:
    # The middleware observes; it must not swallow.
    client = TestClient(_app(), raise_server_exceptions=True)
    with pytest.raises(RuntimeError):
        client.get("/boom")


def test_the_success_path_is_unchanged(caplog: pytest.LogCaptureFixture) -> None:
    client = TestClient(_app())
    with caplog.at_level(logging.INFO, logger="copilot.api.access"):
        r = client.get("/fine")
    assert r.status_code == 200
    assert CORRELATION_ID_HEADER in r.headers
    rec = [x for x in caplog.records if x.message == "http.request"][-1]
    assert rec.http_status == 200 and rec.levelno == logging.INFO  # type: ignore[attr-defined]
