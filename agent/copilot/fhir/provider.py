"""Build the FHIR client with the token provider appropriate to the environment.

Config-driven: when a Backend Services client is configured (a ``client_id`` plus
a private key on disk), use the real ``client_credentials`` + ``private_key_jwt``
flow so the agent reads a live OpenEMR. Otherwise fall back to a static stub bearer
— the offline/test default (the acceptance fake accepts any token; a real OpenEMR
rejects it, which is exactly why live reads require the config below).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from functools import partial
from pathlib import Path
from typing import Any

import httpx

from copilot.auth.service import build_session_token_provider
from copilot.config import Settings
from copilot.domain.primitives import ResourceType, utcnow
from copilot.fhir.auth import (
    BackendServicesTokenProvider,
    OAuthToken,
    ResourceOwnerPasswordTokenProvider,
    StaticTokenProvider,
    TokenProvider,
)
from copilot.fhir.client import FhirClient
from copilot.fhir.write_client import OpenEmrWriteClient

# Minimal system read scopes the poller/rounds path needs, used when
# ``backend_services_scopes`` is left empty.
_DEFAULT_SYSTEM_SCOPES: tuple[str, ...] = (
    "system/Patient.read",
    "system/Observation.read",
    "system/MedicationRequest.read",
    "system/MedicationStatement.read",
    "system/Condition.read",
    "system/AllergyIntolerance.read",
    "system/Encounter.read",
    "system/DiagnosticReport.read",
)


def build_token_provider(settings: Settings) -> TokenProvider:
    """Real Backend Services provider when configured, else a stub bearer."""
    if settings.backend_services_client_id and settings.backend_services_private_key_path:
        pem = Path(settings.backend_services_private_key_path).read_text(encoding="utf-8")
        scopes = tuple(settings.backend_services_scopes.split()) or _DEFAULT_SYSTEM_SCOPES
        return BackendServicesTokenProvider(
            token_url=settings.oauth_token_url,
            client_id=settings.backend_services_client_id,
            private_key_pem=pem,
            scopes=scopes,
            audience=settings.oauth_audience or None,
            http_client_factory=partial(httpx.AsyncClient, verify=settings.tls_verify),
        )
    stub = OAuthToken(
        access_token="stub-serve-token",
        token_type="Bearer",
        expires_at=utcnow() + timedelta(hours=1),
    )
    return StaticTokenProvider(token=stub)


class _PatientMappedFhirClient(FhirClient):
    """FhirClient that maps the agent's integer patient id to the OpenEMR FHIR
    Patient UUID in ``search`` params, via a configured template.

    For local demos against a seed with deterministic UUIDs; a real deployment
    would resolve the UUID from OpenEMR rather than templating it. Only ``search``
    is mapped (the poller's ``count_since`` is a separate, off-by-default path).
    """

    def __init__(
        self,
        base_url: str,
        token_provider: TokenProvider,
        *,
        patient_id_template: str,
        verify: bool = True,
    ) -> None:
        super().__init__(base_url, token_provider, verify=verify)
        self._pid_template = patient_id_template

    async def search(
        self, resource_type: ResourceType, params: Mapping[str, str]
    ) -> dict[str, Any]:
        patient = params.get("patient")
        if patient is not None and patient.isdigit():
            params = {**params, "patient": self._pid_template.format(pid=int(patient))}
        return await super().search(resource_type, params)


def build_fhir_client(settings: Settings) -> FhirClient:
    """FhirClient wired to the environment-appropriate token provider (+ patient map)."""
    provider = build_token_provider(settings)
    return _wrap_fhir_client(settings, provider)


def build_fhir_client_for_session(settings: Settings, session_id: str) -> FhirClient:
    """FhirClient wired to the logged-in physician's delegated session token.

    Interactive request path ONLY — never the poller/background path. The token
    is served from the physician's encrypted server session (a
    :class:`SessionTokenProvider` bound to ``session_id``), so OpenEMR's own
    native audit attributes each read to that individual physician rather than to
    the shared system read client. Reachable only from a smart-mode route that has
    already resolved a live session; ``build_session_token_provider`` reuses the
    exact DB-backed load/save injection the provider expects.
    """
    provider = build_session_token_provider(settings, session_id)
    return _wrap_fhir_client(settings, provider)


def _wrap_fhir_client(settings: Settings, provider: TokenProvider) -> FhirClient:
    """Wrap a token provider in a ``FhirClient`` (+ optional patient-id map)."""
    if settings.fhir_patient_id_template:
        return _PatientMappedFhirClient(
            settings.fhir_base_url,
            provider,
            patient_id_template=settings.fhir_patient_id_template,
            verify=settings.tls_verify,
        )
    return FhirClient(settings.fhir_base_url, provider, verify=settings.tls_verify)


# --- Write path (interactive only — NEVER the poller/background path) -------


class WritebackDisabledError(RuntimeError):
    """Raised when a write provider/client is requested but cannot be built.

    Either write-back is disabled (the master flag) or the write credentials are
    absent. This is the guard that keeps the writable credential out of the
    read-only poller path: the poller never calls these builders, and if any code
    ever did while write-back was off, it fails loudly instead of writing.
    """


# The credentials that MUST be present before a write client can be built.
_REQUIRED_WRITE_SETTINGS: tuple[str, ...] = ("write_client_id", "write_username", "write_password")


def build_write_token_provider(settings: Settings) -> ResourceOwnerPasswordTokenProvider:
    """Password-grant provider for the dedicated write user.

    GUARDED: raises ``WritebackDisabledError`` unless write-back is explicitly
    enabled *and* the write credentials are configured. Only ever called from the
    interactive request path — never the background lifespan/poller, whose system
    token stays read-only.
    """
    if not settings.writeback_enabled:
        raise WritebackDisabledError("write-back is disabled (COPILOT_WRITEBACK_ENABLED=false)")
    missing = [name for name in _REQUIRED_WRITE_SETTINGS if not getattr(settings, name)]
    if missing:
        raise WritebackDisabledError(f"write credentials not configured: {', '.join(missing)}")
    return ResourceOwnerPasswordTokenProvider(
        token_url=settings.oauth_token_url,
        client_id=settings.write_client_id,
        username=settings.write_username,
        password=settings.write_password,
        client_secret=settings.write_client_secret or None,
        scope=settings.write_scopes or None,
        http_client_factory=partial(httpx.AsyncClient, verify=settings.tls_verify),
    )


def build_write_client(settings: Settings) -> OpenEmrWriteClient:
    """Standard-API write client wired to the password-grant provider.

    GUARDED via ``build_write_token_provider`` — see its contract. The write
    client is constructed only in the interactive path; keeping it out of the
    poller is a hard invariant (``research/WRITEBACK_PHASE1_PLAN.md`` §2.4).
    """
    provider = build_write_token_provider(settings)
    return OpenEmrWriteClient(_write_api_base_url(settings), provider, verify=settings.tls_verify)


def build_write_client_for_session(settings: Settings, session_id: str) -> OpenEmrWriteClient:
    """Standard-API write client wired to the physician's delegated session token.

    Interactive request path ONLY — never the poller. GUARDED on
    ``writeback_enabled`` (belt-and-braces with the route's 503) but, unlike the
    disabled-mode password grant, needs NO dedicated write credentials: the
    physician's own SMART token already carries the ``api:oemr user/*.crus`` write
    scopes, so OpenEMR attributes the write to that individual physician. Raises
    ``WritebackDisabledError`` (route → 503) when write-back is off.
    """
    if not settings.writeback_enabled:
        raise WritebackDisabledError("write-back is disabled (COPILOT_WRITEBACK_ENABLED=false)")
    provider = build_session_token_provider(settings, session_id)
    return OpenEmrWriteClient(_write_api_base_url(settings), provider, verify=settings.tls_verify)


def _write_api_base_url(settings: Settings) -> str:
    """The Standard REST API base — explicit, else derived from the FHIR base."""
    if settings.write_api_base_url:
        return settings.write_api_base_url
    fhir_base = settings.fhir_base_url.rstrip("/")
    if fhir_base.endswith("/fhir"):
        return fhir_base[: -len("/fhir")] + "/api"
    raise WritebackDisabledError(
        "write_api_base_url is unset and cannot be derived from fhir_base_url"
    )
