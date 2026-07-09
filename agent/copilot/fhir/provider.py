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

from copilot.config import Settings
from copilot.domain.primitives import ResourceType, utcnow
from copilot.fhir.auth import (
    BackendServicesTokenProvider,
    OAuthToken,
    StaticTokenProvider,
    TokenProvider,
)
from copilot.fhir.client import FhirClient

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
    if settings.fhir_patient_id_template:
        return _PatientMappedFhirClient(
            settings.fhir_base_url,
            provider,
            patient_id_template=settings.fhir_patient_id_template,
            verify=settings.tls_verify,
        )
    return FhirClient(settings.fhir_base_url, provider, verify=settings.tls_verify)
