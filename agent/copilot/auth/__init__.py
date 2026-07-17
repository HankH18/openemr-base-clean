"""Serve-time authorization boundary (UC-6).

A clinician may only converse about a patient on their *established* rounding
list.  The authorized set is the ``ordered_patient_ids`` of the clinician's
persisted rounding cursor (set by ``POST /v1/rounds/start``).  A chat request
is authorized iff that cursor exists *and* the patient is a member of it;
anything else is refused (HTTP 403) rather than answered.

The check itself lives in ``authorization`` (``is_authorized``); the HTTP
surface enforces it in ``copilot.api.routes.chat``.
"""

from copilot.auth.authorization import is_authorized
from copilot.auth.service import (
    AuthConfigError,
    AuthService,
    BeginLogin,
    LoginCallbackError,
    LoginResult,
    ResolvedSession,
    build_session_token_provider,
    ensure_smart_ready,
)

__all__ = [
    "AuthConfigError",
    "AuthService",
    "BeginLogin",
    "LoginCallbackError",
    "LoginResult",
    "ResolvedSession",
    "build_session_token_provider",
    "ensure_smart_ready",
    "is_authorized",
]
