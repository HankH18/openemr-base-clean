"""FastAPI dependencies for per-physician identity resolution.

The single place a route asks "who is this?". Behavior is gated on ``auth_mode``
so the Phase-2 cutover keeps ``disabled`` mode byte-for-byte identical:

- ``smart``    — resolve the ``ClinicianId`` from the opaque session cookie;
  ``401`` when there is no cookie or the session is expired/revoked. Identity is
  the sole source of truth: a request-supplied ``clinician_id`` is validated
  against the session (``403`` on mismatch), never trusted.
- ``disabled`` — fall back to today's request-supplied ``clinician_id`` (body
  field or query string), exactly as the no-login demo does.

Two entry points:

- ``current_clinician`` — a bare FastAPI dependency for routes that carry no
  asserted ``clinician_id`` (or only a query one). No mismatch check.
- ``resolve_acting_clinician`` — called at the top of a handler that DOES carry
  an asserted ``clinician_id`` (body or query), so the ``403``-mismatch check in
  ``smart`` mode can see the asserted id. This is the helper the data routes use.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException, Request

from copilot.auth.service import AuthService, ResolvedSession
from copilot.config import Settings, get_settings
from copilot.domain.primitives import ClinicianId


@dataclass(frozen=True)
class ActingClinician:
    """The resolved acting clinician plus the session that authenticated them.

    ``session_id`` is the ``physician_session`` row key (``sha256`` of the cookie)
    in ``smart`` mode and ``None`` in ``disabled`` mode — the discriminator the
    interactive routes use to decide whether to ride the physician's delegated
    token (per-session client) or the shared system/password path.
    """

    clinician_id: ClinicianId
    session_id: str | None


async def current_clinician(request: Request) -> ClinicianId:
    """Resolve the acting clinician for this request (mode-dependent)."""
    settings = get_settings()
    if settings.auth_mode == "smart":
        return await _from_session(request, settings)
    return _from_request(settings, request)


async def resolve_acting_clinician(
    settings: Settings,
    request: Request,
    asserted_id: int | None,
) -> ClinicianId:
    """Resolve the acting clinician for a data route, honoring the cutover contract.

    - ``disabled`` — trust the request-supplied ``asserted_id`` (body or query),
      exactly as today. When absent, fall back to the query string (``400`` if it
      is missing too).
    - ``smart`` — identity is the session cookie's ``ClinicianId`` (``401`` when
      there is no valid session). The session is authoritative: an ``asserted_id``
      that disagrees with it is a ``403``; a matching or absent one is accepted.
    """
    return (await resolve_acting_context(settings, request, asserted_id)).clinician_id


async def resolve_acting_context(
    settings: Settings,
    request: Request,
    asserted_id: int | None,
) -> ActingClinician:
    """Resolve identity *and* the authenticating session in one pass.

    Same cutover contract as :func:`resolve_acting_clinician` (which delegates
    here), but also carries the ``physician_session`` id so a smart-mode route can
    build the physician's delegated per-session FHIR/write client. Resolves the
    session exactly once — no extra DB read or sliding-window touch beyond what
    :func:`resolve_acting_clinician` already did. In ``disabled`` mode
    ``session_id`` is ``None`` and the route injects nothing (system/password
    path unchanged).
    """
    if settings.auth_mode == "smart":
        session = await _resolve_session_or_401(request, settings)
        if asserted_id is not None and asserted_id != session.clinician_id.value:
            raise HTTPException(
                status_code=403, detail="clinician_id does not match the authenticated session"
            )
        return ActingClinician(session.clinician_id, session.session_id_hash)
    if asserted_id is None:
        return ActingClinician(_from_request(settings, request), None)
    if asserted_id <= 0:
        raise HTTPException(status_code=400, detail="clinician_id must be a positive integer")
    return ActingClinician(ClinicianId(value=asserted_id), None)


async def _from_session(request: Request, settings: Settings) -> ClinicianId:
    return (await _resolve_session_or_401(request, settings)).clinician_id


async def _resolve_session_or_401(request: Request, settings: Settings) -> ResolvedSession:
    """Resolve the live session for this request, or raise ``401``."""
    cookie = request.cookies.get(settings.session_cookie_name)
    if not cookie:
        raise HTTPException(status_code=401, detail="Not authenticated")
    resolved = await AuthService(settings).resolve_session(cookie)
    if resolved is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return resolved


def _from_request(settings: Settings, request: Request) -> ClinicianId:
    raw = request.query_params.get("clinician_id")
    if raw is None:
        raise HTTPException(status_code=400, detail="clinician_id is required")
    try:
        value = int(raw)
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail="clinician_id must be a positive integer"
        ) from exc
    if value <= 0:
        raise HTTPException(status_code=400, detail="clinician_id must be a positive integer")
    return ClinicianId(value=value)
