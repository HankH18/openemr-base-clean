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
  CommittedWrite,
  ConversationMessage,
  DeteriorationAlert,
  DocumentAccepted,
  ObservationSeries,
  ProposedWrite,
  RefreshOutcome,
  RoundView,
  WriteCandidate,
  WriteKind,
} from './types';
import { createMockApi } from './mock';
import { createHttpApi } from './http';
import { resolveApiBase } from './base';

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
  /**
   * Lazily fetch a patient's per-metric time series for the drill-down trend
   * chart. Orthogonal to the verified-claim contract; each point is
   * independently grounded. `metric` is the humanized label derived from the
   * claim (e.g. "Troponin I"). An unknown metric returns an empty series.
   */
  observations(
    clinicianId: number,
    patientId: number,
    metric: string,
  ): Promise<ObservationSeries>;
  /**
   * Step 1 of the physician direct-edit gate: parse a human-typed vital into a
   * verified candidate and return the structured echo-back to confirm against.
   * Throws `WriteDisabledError` (503) when write-back is off, or
   * `WriteRejectedError` (400) carrying the specific violations for a bad
   * value/unit. Does NOT commit anything.
   */
  proposeWrite(
    clinicianId: number,
    patientId: number,
    kind: WriteKind,
    metric: string,
    rawValue: string,
    unit: string,
  ): Promise<ProposedWrite>;
  /**
   * Step 2: commit the exact candidate the physician reviewed. The `candidate`
   * is round-tripped verbatim and the `idempotencyKey` keys the confirm URL so
   * a double-click cannot create a duplicate record.
   */
  confirmWrite(
    clinicianId: number,
    patientId: number,
    candidate: WriteCandidate,
    idempotencyKey: string,
  ): Promise<CommittedWrite>;
  /**
   * Upload one source document (multipart `POST /v1/documents`) for async
   * extraction. Resolves with the 202 acknowledgement; extraction status is
   * polled separately. The mock adapter simulates acceptance offline.
   */
  uploadDocument(patientId: number, file: File, docType?: string): Promise<DocumentAccepted>;
}

export function createApi(): CopilotApi {
  // A set, non-empty VITE_API_BASE_URL selects the live adapter; unset → mock.
  // The base string itself is normalized by resolveApiBase (shared with the
  // auth hook / gate so /v1/auth/* URLs match the data calls).
  const raw = import.meta.env.VITE_API_BASE_URL;
  if (typeof raw === 'string' && raw.trim() !== '') {
    return createHttpApi(resolveApiBase());
  }
  return createMockApi();
}
