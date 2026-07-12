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

from fastapi import HTTPException, Request

from copilot.auth.service import AuthService
from copilot.config import Settings, get_settings
from copilot.domain.primitives import ClinicianId


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
    if settings.auth_mode == "smart":
        session_clinician = await _from_session(request, settings)
        if asserted_id is not None and asserted_id != session_clinician.value:
            raise HTTPException(
                status_code=403, detail="clinician_id does not match the authenticated session"
            )
        return session_clinician
    if asserted_id is None:
        return _from_request(settings, request)
    if asserted_id <= 0:
        raise HTTPException(status_code=400, detail="clinician_id must be a positive integer")
    return ClinicianId(value=asserted_id)


async def _from_session(request: Request, settings: Settings) -> ClinicianId:
    cookie = request.cookies.get(settings.session_cookie_name)
    if not cookie:
        raise HTTPException(status_code=401, detail="Not authenticated")
    resolved = await AuthService(settings).resolve_session(cookie)
    if resolved is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return resolved.clinician_id


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
