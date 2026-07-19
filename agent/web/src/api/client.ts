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
  DocumentDetail,
  ObservationSeries,
  ProposedWrite,
  RefreshOutcome,
  RoundView,
  WriteCandidate,
  WriteKind,
} from './types';
import type { DocType } from './documents';
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
   * The live adapter POSTs to the dedicated `/v1/rounds/jump` endpoint, which
   * lands exactly on the requested patient (not a re-rank). `unseenIds` is
   * consumed only by the mock adapter, which reorders its session directly.
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
   *
   * `clinicianId` is asserted as a `clinician_id` form field — the same
   * identity contract as `getDocument` below (required in disabled mode,
   * match-checked in smart mode), hence the clinician-first argument order
   * shared by every method on this interface.
   */
  uploadDocument(
    clinicianId: number,
    patientId: number,
    file: File,
    docType?: DocType,
  ): Promise<DocumentAccepted>;
  /**
   * Read one uploaded document's ingestion status plus the latest
   * extraction's facts and their document citations
   * (`GET /v1/documents/{id}`). Polled after upload until `status` is
   * terminal ('extracted' | 'failed'). A malformed body normalizes to a safe
   * empty detail; transport failures throw `ApiError` so the poller can retry.
   */
  getDocument(clinicianId: number, documentId: string): Promise<DocumentDetail>;
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
