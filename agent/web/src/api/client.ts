/**
 * The API seam. `createApi()` returns the live HTTP adapter when
 * `VITE_API_BASE_URL` is set, otherwise the mock adapter seeded with the
 * demo cohort. Both implement the same interface; nothing above this
 * module knows which one it is talking to.
 */

import type {
  AdvanceResult,
  ChatRequest,
  ChatResponse,
  ConversationMessage,
  DeteriorationAlert,
  RefreshOutcome,
  RoundView,
} from './types';
import { createMockApi } from './mock';
import { createHttpApi } from './http';

export interface CopilotApi {
  readonly mode: 'mock' | 'live';
  startRound(clinicianId: number, patientIds: number[]): Promise<RoundView>;
  currentCard(clinicianId: number): Promise<RoundView>;
  advance(clinicianId: number, completedPatientId: number): Promise<AdvanceResult>;
  refresh(clinicianId: number): Promise<RefreshOutcome[]>;
  alerts(clinicianId: number): Promise<DeteriorationAlert[]>;
  /**
   * Make `patientId` the current patient without marking anyone done.
   * The service has no dedicated jump endpoint, so the live adapter
   * re-starts the round over the not-yet-seen list (the server re-ranks by
   * acuity, which puts the alerted patient on top); the mock reorders its
   * session directly.
   */
  jumpTo(clinicianId: number, patientId: number, unseenIds: number[]): Promise<RoundView>;
  chat(req: ChatRequest): Promise<ChatResponse>;
  getConversation(conversationId: number): Promise<ConversationMessage[]>;
}

export function createApi(): CopilotApi {
  const base = import.meta.env.VITE_API_BASE_URL;
  if (typeof base === 'string' && base.trim() !== '') {
    return createHttpApi(base.trim().replace(/\/+$/, ''));
  }
  return createMockApi();
}
