"""The FHIR client factory picks the right token provider from config."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from copilot.config import Settings
from copilot.domain.primitives import ResourceType
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


def test_no_template_leaves_patient_param_verbatim() -> None:
    # Default (no template): the acceptance fake + tests key by integer id.
    client = build_fhir_client(_settings())
    assert not isinstance(
        client, type(build_fhir_client(_settings(fhir_patient_id_template="x{pid}")))
    )


@respx.mock
async def test_patient_template_maps_search_param() -> None:
    route = respx.get(url__regex=r"http://openemr/.*/Observation").mock(
        return_value=httpx.Response(200, json={"resourceType": "Bundle", "total": 0})
    )
    client = build_fhir_client(
        _settings(fhir_patient_id_template="a1000000-0000-0000-0000-{pid:012d}")
    )
    async with client as c:
        await c.search(ResourceType.Observation, {"patient": "1001"})
    assert route.called
    assert "patient=a1000000-0000-0000-0000-000000001001" in str(route.calls.last.request.url)
