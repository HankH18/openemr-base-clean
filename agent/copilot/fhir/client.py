"""Async FHIR/REST client.

- Attaches ``Authorization: Bearer …`` sourced from a ``TokenProvider``.
- On 401, refetches the token once and retries.
- Emits the change-detection query (``_lastUpdated=gt{watermark}
  &_summary=count``) as a typed helper (``count_since``).
- Reads raw FHIR JSON — Pydantic parsing lives at call sites, not here,
  so the client is trivial to reuse from ``verification`` re-fetches.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

import httpx

from copilot.domain.primitives import PatientId, ResourceType
from copilot.fhir.auth import TokenAcquisitionError, TokenProvider


class FhirClientError(Exception):
    """Non-2xx response or malformed FHIR body."""


class FhirClient:
    """Small async FHIR reader.

    Not thread-safe (httpx.AsyncClient is task-safe within one event loop,
    which is what FastAPI gives us).  One instance per event loop; the
    caller owns the lifecycle (async context manager).
    """

    def __init__(
        self,
        base_url: str,
        token_provider: TokenProvider,
        *,
        http_client: httpx.AsyncClient | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token_provider = token_provider
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(timeout=timeout)

    async def __aenter__(self) -> "FhirClient":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def read(self, resource_type: ResourceType, resource_id: str) -> dict[str, Any]:
        """Fetch a single resource by ID."""
        return await self._request("GET", f"/{resource_type.value}/{resource_id}")

    async def search(
        self, resource_type: ResourceType, params: Mapping[str, str]
    ) -> dict[str, Any]:
        """FHIR search — returns the Bundle as raw JSON."""
        return await self._request("GET", f"/{resource_type.value}", params=params)

    async def count_since(
        self, resource_type: ResourceType, patient_id: PatientId, since: datetime
    ) -> int:
        """``GET /{Resource}?patient={id}&_lastUpdated=gt{ts}&_summary=count``.

        The change-gate the poller uses.  Empty count ⇒ skip synthesis.
        Nonzero ⇒ pull + hash + maybe re-synthesize.
        """
        params = {
            "patient": str(patient_id),
            "_lastUpdated": f"gt{since.isoformat().replace('+00:00', 'Z')}",
            "_summary": "count",
        }
        body = await self.search(resource_type, params)
        total = body.get("total")
        if not isinstance(total, int):
            raise FhirClientError(
                f"missing/invalid 'total' in count response: {body!r}"
            )
        return total

    async def _request(
        self, method: str, path: str, *, params: Mapping[str, str] | None = None
    ) -> dict[str, Any]:
        url = f"{self._base_url}{path}"

        async def _do(force_refresh: bool) -> httpx.Response:
            token = await self._token_provider.get_token(force=force_refresh)
            headers = {
                "Authorization": f"{token.token_type} {token.access_token}",
                "Accept": "application/fhir+json",
            }
            return await self._client.request(method, url, params=params, headers=headers)

        resp = await _do(force_refresh=False)
        if resp.status_code == 401:
            # One retry with a forced token refresh — handles a
            # server-side revocation between requests.
            try:
                resp = await _do(force_refresh=True)
            except TokenAcquisitionError as exc:
                raise FhirClientError(f"token refresh failed after 401: {exc}") from exc

        if resp.status_code >= 400:
            raise FhirClientError(
                f"FHIR {method} {path} returned status={resp.status_code}"
            )
        try:
            return resp.json()
        except Exception as exc:  # noqa: BLE001 — malformed JSON from FHIR is FhirClientError
            raise FhirClientError(f"FHIR response was not JSON: {exc}") from exc
