/**
 * Live HTTP adapter for the Co-Pilot FastAPI service.
 * Point the app at it with VITE_API_BASE_URL, e.g.
 *   VITE_API_BASE_URL=http://localhost:8000 npm run dev
 */

import type { CopilotApi } from './client';
import {
  normalizeAdvance,
  normalizeAlerts,
  normalizeChat,
  normalizeConversation,
  normalizeRefresh,
  normalizeRoundView,
} from './normalize';
import { ApiError, type ChatRequest } from './types';

async function request(base: string, path: string, init?: RequestInit): Promise<unknown> {
  let response: Response;
  try {
    response = await fetch(`${base}${path}`, {
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      ...init,
    });
  } catch {
    throw new ApiError('Could not reach the record service');
  }
  if (!response.ok) {
    throw new ApiError(`Record service replied ${response.status}`, response.status);
  }
  try {
    return (await response.json()) as unknown;
  } catch {
    throw new ApiError('Record service returned malformed JSON');
  }
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

    async jumpTo(clinicianId, patientId, unseenIds) {
      // No dedicated endpoint: re-establish the round over the unseen list.
      // The server ranks by acuity; the alerted (highest-acuity) patient
      // comes back on top.
      const ids = [patientId, ...unseenIds.filter((id) => id !== patientId)];
      const raw = await post('/v1/rounds/start', {
        clinician_id: clinicianId,
        patient_ids: ids,
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
  };
}
