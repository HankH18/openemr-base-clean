"""Physician identity — from OpenEMR's ``id_token``/userinfo to a ``ClinicianId``.

Two responsibilities (``PRODUCTION_GRADE_PLAN.md`` §1.6):

1. **Parse** the OIDC identity claims returned by the SMART ``authorization_code``
   exchange into a typed :class:`ParsedIdentity` — chiefly ``fhirUser`` (the
   Practitioner reference that IS the physician's identity) and ``sub``.
2. **Map + auto-provision** — resolve ``fhirUser`` to the stable integer
   ``ClinicianId`` via the ``clinician`` table, minting a row on first login and
   reusing it thereafter, so the int-keyed tables never change.

The ``id_token`` arrives over the authenticated back-channel token response
(TLS + client auth), so its claims are trusted without a separate JWKS
signature check here; we decode the payload to read them. (A public,
browser-delivered token would require signature verification — this is not
that.)
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from copilot.domain.primitives import ClinicianId
from copilot.memory.repository import MemoryRepository


class IdentityError(RuntimeError):
    """The identity token/userinfo carried no usable ``fhirUser`` claim."""


@dataclass(frozen=True)
class ParsedIdentity:
    """The physician identity extracted from OIDC claims."""

    fhir_user: str
    subject: str | None
    display_name: str | None
    username: str | None
    npi: str | None


def _decode_jwt_claims(token: str) -> dict[str, Any]:
    """Decode a JWT's payload segment (no signature verification — see module doc)."""
    parts = token.split(".")
    if len(parts) < 2:
        raise IdentityError("id_token is not a well-formed JWT")
    payload_b64 = parts[1]
    padding = "=" * (-len(payload_b64) % 4)
    try:
        raw = base64.urlsafe_b64decode(payload_b64 + padding)
        claims = json.loads(raw)
    except (binascii.Error, ValueError) as exc:
        raise IdentityError("id_token payload could not be decoded") from exc
    if not isinstance(claims, dict):
        raise IdentityError("id_token payload is not a JSON object")
    return claims


def _first_str(claims: Mapping[str, Any], *keys: str) -> str | None:
    """Return the first claim whose value is a non-empty string."""
    for key in keys:
        value = claims.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def parse_identity(
    *,
    id_token: str | None,
    userinfo: Mapping[str, Any] | None = None,
) -> ParsedIdentity:
    """Extract a :class:`ParsedIdentity`, preferring userinfo over id_token claims.

    Raises :class:`IdentityError` when neither source yields a ``fhirUser`` — the
    callback maps this to a generic login-error redirect, never a 500.
    """
    claims: dict[str, Any] = {}
    if id_token:
        claims.update(_decode_jwt_claims(id_token))
    if userinfo:
        claims.update(userinfo)

    fhir_user = _first_str(claims, "fhirUser", "fhir_user")
    if fhir_user is None:
        raise IdentityError("no fhirUser claim in id_token/userinfo")

    return ParsedIdentity(
        fhir_user=fhir_user,
        subject=_first_str(claims, "sub"),
        display_name=_first_str(claims, "name", "preferred_username", "given_name"),
        username=_first_str(claims, "preferred_username", "username"),
        npi=_first_str(claims, "npi"),
    )


async def resolve_clinician(
    repo: MemoryRepository,
    identity: ParsedIdentity,
    *,
    now: datetime,
) -> ClinicianId:
    """Map ``fhirUser`` → ``ClinicianId``, auto-provisioning on first login.

    A new row mints the integer surrogate (``ClinicianId.value``); a returning
    physician reuses the existing id. Either way ``last_login_at`` is stamped.
    This is what replaces the hardcoded demo clinician when SMART is enabled.
    """
    row = await repo.get_clinician_by_fhir_user(identity.fhir_user)
    if row is None:
        row = await repo.create_clinician(
            fhir_user=identity.fhir_user,
            openemr_username=identity.username,
            display_name=identity.display_name,
            npi=identity.npi,
        )
    await repo.set_clinician_last_login(row.id, now)
    return ClinicianId(value=row.id)
