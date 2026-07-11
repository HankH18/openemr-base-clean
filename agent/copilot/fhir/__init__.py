"""FHIR/REST client + OAuth token acquisition.

The single place that talks to OpenEMR (ARCHITECTURE §"Components").  Two
OAuth actors live here:

- ``SmartAppLaunchTokenProvider`` — physician-delegated auth-code flow.
- ``BackendServicesTokenProvider`` — ``client_credentials`` with a signed
  JWT assertion (SMART Backend Services, ``system/*.read`` scopes).

Callers pass a provider into ``FhirClient``; the client attaches
``Authorization: Bearer …`` for every request and refreshes on 401.
"""

from copilot.fhir.auth import (
    BackendServicesTokenProvider,
    OAuthToken,
    ResourceOwnerPasswordTokenProvider,
    SmartAppLaunchTokenProvider,
    StaticTokenProvider,
    TokenProvider,
)
from copilot.fhir.client import FhirClient, FhirClientError
from copilot.fhir.write_client import OpenEmrWriteClient, OpenEmrWriteError

__all__ = [
    "BackendServicesTokenProvider",
    "FhirClient",
    "FhirClientError",
    "OAuthToken",
    "OpenEmrWriteClient",
    "OpenEmrWriteError",
    "ResourceOwnerPasswordTokenProvider",
    "SmartAppLaunchTokenProvider",
    "StaticTokenProvider",
    "TokenProvider",
]
