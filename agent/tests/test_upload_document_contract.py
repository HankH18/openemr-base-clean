"""upload_document must match OpenEMR's ACTUAL document-create contract.

Verified against OpenEMR's own source, not assumed. `DocumentRestController::
postWithPath` returns `responseHandler($serviceResult, null, 200)`, and
`DocumentService::insertAtPath` returns a bare `true` on success / `false` on
failure (which responseHandler turns into 404 + empty body). The route also reads
the category from `$request->query->get('path')` — a QUERY parameter.

Every one of those disagreed with this client, and each failed silently:
  * we required 201, so a SUCCESSFUL upload (200) raised;
  * we demanded a parseable id from a body that is literally `true`;
  * we sent `path` as form data, so OpenEMR saw null and isValidPath rejected it.

Failure mode guarded: "store the source document in OpenEMR" — an explicit spec
requirement — cannot work. It was masked only because write-back is off, so
DerivedOnlyUploader is substituted and nothing ever exercised the real client.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import httpx
import pytest

from copilot.domain.primitives import PatientId, utcnow
from copilot.fhir.auth import OAuthToken
from copilot.fhir.write_client import (
    OPENEMR_NO_HANDLE,
    OpenEmrWriteClient,
    OpenEmrWriteError,
)

_PID = PatientId(value=1001)


class _FakeTokens:
    """Minimal TokenProvider double — the write path only needs a bearer."""

    async def get_token(self, force: bool = False) -> OAuthToken:
        return OAuthToken(
            access_token="tok",
            token_type="Bearer",
            expires_at=utcnow() + timedelta(hours=1),
        )


def _client(handler: Any) -> OpenEmrWriteClient:
    transport = httpx.MockTransport(handler)
    return OpenEmrWriteClient(
        "http://oe.test/apis/default/api",
        _FakeTokens(),  # type: ignore[arg-type]
        http_client=httpx.AsyncClient(transport=transport),
    )


@pytest.mark.parametrize("code", [200, 201])
def test_a_2xx_with_bare_true_is_success_not_a_failure(code: int) -> None:
    # 200 is what OpenEMR's DocumentRestController actually returns, and requiring
    # 201 made a SUCCESSFUL upload raise. Requiring only 200 would be the same
    # brittleness inverted — both codes mean "created", and which one a deployment
    # sends is not worth being dogmatic about.
    import anyio

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(code, json=True)

    result = anyio.run(_client(handler).upload_document, _PID, b"%PDF-1.4\n")
    assert result == OPENEMR_NO_HANDLE, "a confirmed upload with no id yields the sentinel"


def test_a_non_success_status_still_fails_closed() -> None:
    # Accepting a range is not laxity: anything outside it still raises.
    import anyio

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(202, json=True)

    with pytest.raises(OpenEmrWriteError):
        anyio.run(_client(handler).upload_document, _PID, b"%PDF-1.4\n")


def test_the_category_rides_the_query_string_not_the_body() -> None:
    # The route reads $request->query->get('path'); as form data it arrives null
    # and isValidPath(null) rejects the upload.
    import anyio

    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json=True)

    anyio.run(
        lambda: _client(handler).upload_document(
            _PID, b"%PDF-1.4\n", category="Lab Reports"
        )
    )
    assert "path=" in seen["url"], f"category must be a query param; got {seen['url']}"
    assert "Lab%20Reports" in seen["url"] or "Lab Reports" in seen["url"]


def test_a_404_is_the_real_failure_signal_and_fails_closed() -> None:
    # insertAtPath -> false -> responseHandler -> 404 + empty body.
    import anyio

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, content=b"")

    with pytest.raises(OpenEmrWriteError):
        anyio.run(_client(handler).upload_document, _PID, b"%PDF-1.4\n")


def test_an_explicit_false_body_fails_closed() -> None:
    # Defensive: a 200 whose payload is false is not a success.
    import anyio

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=False)

    with pytest.raises(OpenEmrWriteError):
        anyio.run(_client(handler).upload_document, _PID, b"%PDF-1.4\n")


def test_an_id_is_still_used_when_a_deployment_returns_one() -> None:
    # Opportunistic: don't lose a real handle if an envelope carries one.
    import anyio

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"id": 77}})

    assert anyio.run(_client(handler).upload_document, _PID, b"%PDF-1.4\n") == "77"
