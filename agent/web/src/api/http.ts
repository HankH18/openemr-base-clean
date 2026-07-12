/**
 * Live HTTP adapter for the Co-Pilot FastAPI service.
 * Point the app at it with VITE_API_BASE_URL, e.g.
 *   VITE_API_BASE_URL=http://localhost:8000 npm run dev
 */

import type { CopilotApi } from './client';
import {
  extractDisabledMessage,
  extractWriteErrors,
  normalizeAdvance,
  normalizeAlerts,
  normalizeChat,
  normalizeCommittedWrite,
  normalizeConversation,
  normalizeObservationSeries,
  normalizeProposedWrite,
  normalizeRefresh,
  normalizeRoundView,
} from './normalize';
import { ApiError, WriteDisabledError, WriteRejectedError, type ChatRequest } from './types';
import { getCsrfToken, shouldRedirectOn401 } from './session';

/**
 * CSRF header for state-changing methods, when a token is available (SMART
 * mode, authenticated). Empty in disabled/mock mode, so requests are byte-for-
 * byte what they were before auth existed.
 */
function csrfHeader(method?: string): Record<string, string> {
  const verb = (method ?? 'GET').toUpperCase();
  if (verb === 'POST' || verb === 'PUT' || verb === 'DELETE' || verb === 'PATCH') {
    const token = getCsrfToken();
    if (token !== null) {
      return { 'X-CSRF-Token': token };
    }
  }
  return {};
}

/**
 * A 401 on an authenticated SMART session means it lapsed — bounce to the OAuth
 * login. Gated on that state (see session.ts) so a 401 on an auth-disabled
 * deploy, or for a not-yet-signed-in SMART user, stays an ordinary ApiError and
 * never hijacks the sign-in gate.
 */
function handleUnauthorized(base: string, status: number): void {
  if (status === 401 && shouldRedirectOn401()) {
    window.location.href = `${base}/v1/auth/login`;
  }
}

async function request(base: string, path: string, init?: RequestInit): Promise<unknown> {
  let response: Response;
  try {
    response = await fetch(`${base}${path}`, {
      credentials: 'include',
      ...init,
      headers: {
        'Content-Type': 'application/json',
        Accept: 'application/json',
        ...csrfHeader(init?.method),
        ...((init?.headers as Record<string, string> | undefined) ?? {}),
      },
    });
  } catch {
    throw new ApiError('Could not reach the record service');
  }
  if (!response.ok) {
    handleUnauthorized(base, response.status);
    throw new ApiError(`Record service replied ${response.status}`, response.status);
  }
  try {
    return (await response.json()) as unknown;
  } catch {
    throw new ApiError('Record service returned malformed JSON');
  }
}

/**
 * POST for the write endpoints. Unlike `request`, it reads the response body
 * even on a non-2xx so the two contract failure modes surface as typed errors:
 * 503 → `WriteDisabledError` (write-back off), 400/422 → `WriteRejectedError`
 * carrying the verdict's specific violations. Everything else is a generic
 * ApiError — a write whose success cannot be confirmed is never assumed committed.
 */
async function requestWrite(base: string, path: string, body: unknown): Promise<unknown> {
  let response: Response;
  try {
    response = await fetch(`${base}${path}`, {
      method: 'POST',
      credentials: 'include',
      headers: {
        'Content-Type': 'application/json',
        Accept: 'application/json',
        ...csrfHeader('POST'),
      },
      body: JSON.stringify(body),
    });
  } catch {
    throw new ApiError('Could not reach the record service');
  }

  let payload: unknown = null;
  try {
    payload = (await response.json()) as unknown;
  } catch {
    payload = null;
  }

  if (response.status === 503) {
    throw new WriteDisabledError(extractDisabledMessage(payload));
  }
  if (response.status === 400 || response.status === 422) {
    throw new WriteRejectedError(extractWriteErrors(payload));
  }
  if (!response.ok) {
    handleUnauthorized(base, response.status);
    throw new ApiError(`Record service replied ${response.status}`, response.status);
  }
  if (payload === null) {
    throw new ApiError('Record service returned malformed JSON');
  }
  return payload;
}

export function createHttpApi(base: string): CopilotApi {
  const get = (path: string): Promise<unknown> => request(base, path);
  const post = (path: string, body: unknown): Promise<unknown> =>
    request(base, path, { method: 'POST', body: JSON.stringify(body) });

  return {
    mode: 'live',

    async startRound(clinicianId, patientIds) {
      const raw = await post('/v1/rounds/start', {
        clinician_id: clinicianId,
        patient_ids: patientIds,
      });
      return normalizeRoundView(raw);
    },

    async currentCard(clinicianId) {
      const raw = await get(`/v1/rounds/current?clinician_id=${clinicianId}`);
      return normalizeRoundView(raw);
    },

    async advance(clinicianId, completedPatientId) {
      const raw = await post('/v1/rounds/advance', {
        clinician_id: clinicianId,
        completed_patient_id: completedPatientId,
      });
      return normalizeAdvance(raw);
    },

    async refresh(clinicianId) {
      const raw = await post('/v1/rounds/refresh', { clinician_id: clinicianId });
      return normalizeRefresh(raw);
    },

    async alerts(clinicianId) {
      const raw = await get(`/v1/rounds/alerts?clinician_id=${clinicianId}`);
      return normalizeAlerts(raw);
    },

    async jumpTo(clinicianId, patientId) {
      // Reposition the durable cursor to the requested patient, reusing the
      // summaries synthesized at start. Instant and exact — it lands on the
      // patient asked for (unlike re-ranking, where an equal-acuity tie could
      // surface someone else), so the "Jump to" offer always works.
      const raw = await post('/v1/rounds/jump', {
        clinician_id: clinicianId,
        patient_id: patientId,
      });
      return normalizeRoundView(raw);
    },

    async chat(req: ChatRequest) {
      const raw = await post('/v1/chat', req);
      return normalizeChat(raw);
    },

    async getConversation(conversationId) {
      const raw = await get(`/v1/conversations/${conversationId}`);
      return normalizeConversation(raw);
    },

    async observations(clinicianId, patientId, metric) {
      const raw = await get(
        `/v1/patients/${patientId}/observations?metric=${encodeURIComponent(metric)}` +
          `&clinician_id=${clinicianId}`,
      );
      return normalizeObservationSeries(raw);
    },

    async proposeWrite(clinicianId, patientId, kind, metric, rawValue, unit) {
      const raw = await requestWrite(base, '/v1/writes', {
        clinician_id: clinicianId,
        patient_id: patientId,
        kind,
        metric,
        raw_value: rawValue,
        unit,
      });
      return normalizeProposedWrite(raw);
    },

    async confirmWrite(clinicianId, patientId, candidate, idempotencyKey) {
      const raw = await requestWrite(
        base,
        `/v1/writes/${encodeURIComponent(idempotencyKey)}/confirm`,
        { clinician_id: clinicianId, patient_id: patientId, candidate },
      );
      return normalizeCommittedWrite(raw);
    },
  };
}
