"""The FHIR client factory picks the right token provider from config."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from copilot.config import Settings
from copilot.fhir.auth import BackendServicesTokenProvider, StaticTokenProvider
from copilot.fhir.client import FhirClient
from copilot.fhir.provider import build_fhir_client, build_token_provider


def _settings(**overrides: Any) -> Settings:
    # _env_file=None keeps this hermetic regardless of any local .env.
    return Settings(_env_file=None, **overrides)


def _write_key(tmp_path: Path) -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    path = tmp_path / "backend.pem"
    path.write_bytes(pem)
    return str(path)


def test_default_settings_use_stub_provider() -> None:
    assert isinstance(build_token_provider(_settings()), StaticTokenProvider)


def test_client_id_without_key_still_stubs() -> None:
    # A client id alone (no private key on disk) must NOT flip to the live flow.
    assert isinstance(
        build_token_provider(_settings(backend_services_client_id="cid")),
        StaticTokenProvider,
    )


def test_backend_config_uses_backend_provider(tmp_path: Path) -> None:
    provider = build_token_provider(
        _settings(
            backend_services_client_id="cid-123",
            backend_services_private_key_path=_write_key(tmp_path),
            oauth_token_url="https://oe.test/oauth2/token",
        )
    )
    assert isinstance(provider, BackendServicesTokenProvider)
    assert provider.client_id == "cid-123"
    assert provider.token_url == "https://oe.test/oauth2/token"
    assert provider.scopes  # default read set applied when scopes unset


def test_explicit_scopes_are_parsed(tmp_path: Path) -> None:
    provider = build_token_provider(
        _settings(
            backend_services_client_id="cid",
            backend_services_private_key_path=_write_key(tmp_path),
            backend_services_scopes="system/Patient.read system/Observation.read",
        )
    )
    assert isinstance(provider, BackendServicesTokenProvider)
    assert provider.scopes == ("system/Patient.read", "system/Observation.read")


def test_build_fhir_client_returns_client() -> None:
    assert isinstance(build_fhir_client(_settings()), FhirClient)
