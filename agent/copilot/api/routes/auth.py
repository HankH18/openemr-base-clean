"""Per-physician SMART login API — the ``/v1/auth/*`` surface.

Implements the BFF login contract (``PRODUCTION_GRADE_PLAN.md`` §10):

- ``GET  /v1/auth/login``    → ``302`` to OpenEMR authorize (persists a login_txn).
- ``GET  /v1/auth/callback`` → validate state, exchange code, ``302`` to the SPA
  with ``Set-Cookie: af_session=…; HttpOnly; Secure; SameSite=Lax``. Any failure
  redirects to ``/?login_error=…`` — never a 500, never a leaked reason.
- ``GET  /v1/auth/me``       → ``200`` identity JSON when authed, else ``401``.
- ``POST /v1/auth/logout``   → ``204``; clears the cookie and best-effort revokes.

Every endpoint is inert while ``auth_mode="disabled"``: ``login``/``callback``
report ``404`` (feature off), ``me`` returns ``401`` (no session concept), and
``logout`` just clears any cookie. So the no-login demo is byte-for-byte
unchanged.

Mounted automatically by ``copilot.api.app.register_routers`` (module-level
``router``); no edit to ``app.py`` is required.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse

from copilot.auth.service import AuthConfigError, AuthService, LoginCallbackError
from copilot.auth.session import clear_session_cookie, set_session_cookie
from copilot.config import Settings, get_settings

router = APIRouter(prefix="/v1/auth", tags=["auth"])


def _service(settings: Settings) -> AuthService:
    return AuthService(settings)


def _require_smart(settings: Settings) -> None:
    if settings.auth_mode != "smart":
        raise HTTPException(status_code=404, detail="SMART login is not enabled")


def _safe_next(raw: str | None) -> str | None:
    """Accept only a same-origin relative path as the post-login target.

    Rejects absolute URLs and protocol-relative (``//host``) values so ``next``
    can never be used as an open redirect.
    """
    if raw and raw.startswith("/") and not raw.startswith("//"):
        return raw
    return None


def _login_error_redirect(reason: str) -> RedirectResponse:
    """A generic, same-origin login-error redirect — never leaks internal detail."""
    return RedirectResponse(url=f"/?login_error={reason}", status_code=302)


@router.get("/login", summary="Begin per-physician SMART login (redirect to OpenEMR)")
async def login(request: Request) -> RedirectResponse:
    settings = get_settings()
    _require_smart(settings)
    try:
        begin = await _service(settings).begin_login(_safe_next(request.query_params.get("next")))
    except AuthConfigError as exc:
        # Misconfigured deployment (e.g. non-https origin) — clear, not a 500.
        raise HTTPException(status_code=503, detail="SMART login is not configured") from exc
    return RedirectResponse(url=begin.authorize_url, status_code=302)


@router.get("/callback", summary="OAuth redirect target — exchange the code and set the session")
async def callback(request: Request) -> RedirectResponse:
    settings = get_settings()
    _require_smart(settings)
    code = request.query_params.get("code")
    state = request.query_params.get("state")
    if not code or not state:
        return _login_error_redirect("missing_parameters")
    try:
        result = await _service(settings).complete_login(code, state)
    except (LoginCallbackError, AuthConfigError):
        return _login_error_redirect("login_failed")

    response = RedirectResponse(url=result.redirect_target, status_code=302)
    set_session_cookie(
        response,
        name=settings.session_cookie_name,
        value=result.cookie_value,
        max_age=result.max_age,
    )
    return response


@router.get("/me", summary="Current physician identity for the session cookie")
async def me(request: Request) -> JSONResponse:
    settings = get_settings()
    cookie = request.cookies.get(settings.session_cookie_name)
    if settings.auth_mode != "smart" or not cookie:
        return JSONResponse(status_code=401, content={"detail": "Not authenticated"})
    resolved = await _service(settings).resolve_session(cookie)
    if resolved is None:
        return JSONResponse(status_code=401, content={"detail": "Not authenticated"})
    body: dict[str, Any] = {
        "clinician_id": resolved.clinician_id.value,
        "display_name": resolved.display_name,
        "fhir_user": resolved.fhir_user,
        "expires_at": resolved.expires_at.isoformat(),
        "csrf_token": resolved.csrf_token,
    }
    return JSONResponse(content=body)


@router.post("/logout", status_code=204, summary="Revoke the session and clear the cookie")
async def logout(request: Request) -> Response:
    settings = get_settings()
    cookie = request.cookies.get(settings.session_cookie_name)
    if cookie and settings.auth_mode == "smart":
        await _service(settings).logout(cookie)
    response = Response(status_code=204)
    clear_session_cookie(response, name=settings.session_cookie_name)
    return response
