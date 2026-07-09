"""Build the FHIR client with the token provider appropriate to the environment.

Config-driven: when a Backend Services client is configured (a ``client_id`` plus
a private key on disk), use the real ``client_credentials`` + ``private_key_jwt``
flow so the agent reads a live OpenEMR. Otherwise fall back to a static stub bearer
— the offline/test default (the acceptance fake accepts any token; a real OpenEMR
rejects it, which is exactly why live reads require the config below).
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from copilot.config import Settings
from copilot.domain.primitives import utcnow
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
        )
    stub = OAuthToken(
        access_token="stub-serve-token",
        token_type="Bearer",
        expires_at=utcnow() + timedelta(hours=1),
    )
    return StaticTokenProvider(token=stub)


def build_fhir_client(settings: Settings) -> FhirClient:
    """FhirClient wired to the environment-appropriate token provider."""
    return FhirClient(settings.fhir_base_url, build_token_provider(settings))
