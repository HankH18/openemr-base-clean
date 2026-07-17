import { useEffect, useState } from 'react';
import type { CopilotApi } from '../api/client';
import { resolveApiBase } from '../api/base';
import { setSession } from '../api/session';
import { CLINICIAN_ID } from '../census';

/**
 * Boot-time auth probe implementing the AUTH CUTOVER CONTRACT.
 *
 * - Mock/offline mode → resolves synchronously to a disabled-equivalent state
 *   with NO network call, so the current demo is byte-for-byte unchanged.
 * - Live mode → GET /v1/auth/status. `disabled` behaves like the demo (clinician
 *   `CLINICIAN_ID`); `smart` gates on `authenticated`, fetching /v1/auth/me for
 *   the real identity + CSRF token when signed in.
 * - Any probe failure fails SAFE to disabled behavior so an unreachable or
 *   not-yet-deployed /v1/auth/* endpoint never breaks the current app.
 */

export type AuthMode = 'disabled' | 'smart';
export type AuthStatus = 'loading' | 'ready' | 'error';

export interface AuthState {
  authMode: AuthMode;
  authenticated: boolean;
  clinicianId: number;
  displayName: string | null;
  csrfToken: string | null;
  status: AuthStatus;
}

interface StatusResponse {
  authMode: AuthMode;
  authenticated: boolean;
}

interface MeResponse {
  clinicianId: number;
  displayName: string;
  csrfToken: string | null;
}

function disabledState(status: AuthStatus): AuthState {
  return {
    authMode: 'disabled',
    authenticated: false,
    clinicianId: CLINICIAN_ID,
    displayName: null,
    csrfToken: null,
    status,
  };
}

async function getJson(base: string, path: string): Promise<unknown> {
  const response = await fetch(`${base}${path}`, {
    credentials: 'include',
    headers: { Accept: 'application/json' },
  });
  if (!response.ok) {
    throw new Error(`auth ${path} replied ${response.status}`);
  }
  return (await response.json()) as unknown;
}

function asRecord(raw: unknown): Record<string, unknown> {
  if (typeof raw !== 'object' || raw === null) {
    throw new Error('auth response is not an object');
  }
  return raw as Record<string, unknown>;
}

function parseStatus(raw: unknown): StatusResponse {
  const obj = asRecord(raw);
  const mode = obj['auth_mode'];
  if (mode !== 'disabled' && mode !== 'smart') {
    throw new Error('auth_mode is not "disabled" or "smart"');
  }
  return { authMode: mode, authenticated: obj['authenticated'] === true };
}

function parseMe(raw: unknown): MeResponse {
  const obj = asRecord(raw);
  const clinicianId = obj['clinician_id'];
  if (typeof clinicianId !== 'number') {
    throw new Error('clinician_id is not a number');
  }
  const displayName = obj['display_name'];
  const csrfToken = obj['csrf_token'];
  return {
    clinicianId,
    displayName: typeof displayName === 'string' ? displayName : `Clinician ${clinicianId}`,
    csrfToken: typeof csrfToken === 'string' ? csrfToken : null,
  };
}

export function useAuth(api: CopilotApi): AuthState {
  // Mock mode resolves synchronously (ready/disabled) so the login gate never
  // flashes and no probe fires; live mode starts in loading and probes below.
  const [state, setState] = useState<AuthState>(() =>
    disabledState(api.mode === 'mock' ? 'ready' : 'loading'),
  );

  useEffect(() => {
    if (api.mode === 'mock') {
      setSession({ authenticatedSmart: false, csrfToken: null });
      return;
    }

    let cancelled = false;
    const base = resolveApiBase();

    void (async () => {
      let status: StatusResponse;
      try {
        status = parseStatus(await getJson(base, '/v1/auth/status'));
      } catch {
        // Cannot determine the mode — fail safe to disabled so the current
        // (auth-off) deploy keeps working.
        if (!cancelled) {
          setSession({ authenticatedSmart: false, csrfToken: null });
          setState(disabledState('error'));
        }
        return;
      }

      if (status.authMode === 'disabled') {
        if (!cancelled) {
          setSession({ authenticatedSmart: false, csrfToken: null });
          setState(disabledState('ready'));
        }
        return;
      }

      // SMART mode is active from here on.
      if (!status.authenticated) {
        if (!cancelled) {
          setSession({ authenticatedSmart: false, csrfToken: null });
          setState({
            authMode: 'smart',
            authenticated: false,
            clinicianId: CLINICIAN_ID,
            displayName: null,
            csrfToken: null,
            status: 'ready',
          });
        }
        return;
      }

      try {
        const me = parseMe(await getJson(base, '/v1/auth/me'));
        if (!cancelled) {
          setSession({ authenticatedSmart: true, csrfToken: me.csrfToken });
          setState({
            authMode: 'smart',
            authenticated: true,
            clinicianId: me.clinicianId,
            displayName: me.displayName,
            csrfToken: me.csrfToken,
            status: 'ready',
          });
        }
      } catch {
        // Session lapsed between /status and /me — show the sign-in gate.
        if (!cancelled) {
          setSession({ authenticatedSmart: false, csrfToken: null });
          setState({
            authMode: 'smart',
            authenticated: false,
            clinicianId: CLINICIAN_ID,
            displayName: null,
            csrfToken: null,
            status: 'ready',
          });
        }
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [api]);

  return state;
}
