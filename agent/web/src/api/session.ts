/**
 * Auth/session bridge between the React auth hook (useAuth) and the low-level
 * fetch layer (http.ts). The HTTP adapter is created once and memoized, so it
 * cannot re-read React state; this tiny module singleton carries the two facts
 * the fetch layer needs — the current CSRF token, and whether a 401 should
 * bounce to re-login — plus the login/logout navigation helpers.
 *
 * In auth-disabled and mock/offline mode the token stays null and 401-redirect
 * stays off, so the fetch layer behaves exactly as it did before auth existed
 * (no CSRF header, no redirect — a 401 is an ordinary ApiError).
 */

import { resolveApiBase } from './base';

let csrfToken: string | null = null;
// True ONLY for an authenticated SMART session. A 401 then means the session
// lapsed → re-login. An unauthenticated SMART user is handled by the LoginGate,
// so their requests must NOT auto-redirect (that would hijack the sign-in
// screen); a disabled/mock 401 is just an ordinary failure.
let redirectOn401 = false;

/** Set by useAuth once /v1/auth/status (and, when authed, /me) resolves. */
export function setSession(next: { authenticatedSmart: boolean; csrfToken: string | null }): void {
  redirectOn401 = next.authenticatedSmart;
  csrfToken = next.csrfToken;
}

export function getCsrfToken(): string | null {
  return csrfToken;
}

export function shouldRedirectOn401(): boolean {
  return redirectOn401;
}

export function loginUrl(): string {
  return `${resolveApiBase()}/v1/auth/login`;
}

export function redirectToLogin(): void {
  window.location.href = loginUrl();
}

/**
 * POST /v1/auth/logout (credentials + CSRF), then reload. Best-effort: a failed
 * request must not strand the clinician — the reload re-probes /v1/auth/status,
 * which re-gates to the sign-in screen once the server-side session is gone.
 */
export async function logout(): Promise<void> {
  const base = resolveApiBase();
  const headers: Record<string, string> = { Accept: 'application/json' };
  if (csrfToken !== null) {
    headers['X-CSRF-Token'] = csrfToken;
  }
  try {
    await fetch(`${base}/v1/auth/logout`, {
      method: 'POST',
      credentials: 'include',
      headers,
    });
  } catch {
    /* best effort — reload re-probes auth status regardless */
  }
  window.location.reload();
}
