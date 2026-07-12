"""FastAPI dependencies for per-physician identity resolution.

``current_clinician`` is the single place a route asks "who is this?". Its
behavior is gated on ``auth_mode`` so the cutover (Phase 2) is a one-line swap
per route with no behavior change while disabled:

- ``smart``    — resolve the ``ClinicianId`` from the opaque session cookie;
  ``401`` when there is no cookie or the session is expired/revoked. Identity is
  the sole source of truth (a body/query ``clinician_id`` is authorization-
  irrelevant in this mode).
- ``disabled`` — fall back to today's request-supplied ``clinician_id`` (read
  from the query string here), exactly as the no-login demo does. Provided now
  so Phase 2 can adopt the dependency; it is NOT yet wired into existing routes.
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
