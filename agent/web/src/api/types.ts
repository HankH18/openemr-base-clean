/**
 * Domain types mirrored from the Co-Pilot service contracts
 * (agent/copilot/domain/contracts.py). Every clinical claim carries a
 * source_ref — the fail-closed rule: no source, no claim.
 */

/** Structured pointer to the record a claim was extracted from. */
export interface SourceRef {
  resource_type: string;
  resource_id: string;
  field: string;
  /** Verbatim value from the source record — what verification compares against. */
  value: string;
}

/** One assertion in a summary or a chat answer. */
export interface Claim {
  text: string;
  source_ref: SourceRef;
}

export interface Freshness {
  /** ISO timestamp of the synthesis watermark. */
  as_of: string;
  age_seconds: number;
  stale: boolean;
}

/** What the round loop hands the UI for one patient. */
export interface PatientCard {
  patient_id: number;
  summary_claims: Claim[];
  changes_since_last_seen: Claim[];
  /** 0–10; drives the visit order. */
  acuity_score: number;
  rank_reason: string;
  freshness: Freshness;
}

export interface RoundView {
  current: PatientCard;
  /** Full planned visiting order (patient ids, sickest first). */
  order: number[];
}

export type AdvanceResult = { done: true } | RoundView;

export interface RefreshOutcome {
  patient_id: number;
  outcome: string;
}

export interface DeteriorationAlert {
  patient_id: number;
  reason: string;
}

export type VerificationAction = 'served' | 'withheld' | 'degraded';

export interface Verification {
  action: VerificationAction;
  passed: boolean;
}

export interface ChatRequest {
  clinician_id: number;
  patient_id: number;
  message: string;
  conversation_id?: number;
  correlation_id?: string;
}

export interface ChatResponse {
  answer: string;
  claims: Claim[];
  verification: Verification;
  conversation_id: number;
  correlation_id: string;
}

export interface ConversationMessage {
  role: string;
  content: string;
}

/** Raised by adapters when the service replies with a non-2xx or a bad shape. */
export class ApiError extends Error {
  public readonly status: number | null;

  public constructor(message: string, status: number | null = null) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}
