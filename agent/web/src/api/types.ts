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
  /**
   * Clinically meaningful time of the cited resource — authoredOn
   * (MedicationRequest) or effectiveDateTime (Observation). Grounded, but
   * NOT part of the value-match gate. Absent on timestamp-less claims.
   */
  timestamp?: string | null;
}

/**
 * Record-grounded severity of an observation claim, from its abnormal flag.
 * Absent (neutral) on non-observation claims and on any claim whose flag can't
 * be read.
 */
export type ClaimSeverity = 'normal' | 'warning' | 'critical';

/**
 * Whether the latest reading is moving toward ('improving') or away from
 * ('worsening') its reference range; 'steady' when in-range/unchanged. Absent
 * when it can't be judged (no prior reading, non-numeric, or no range).
 */
export type TrendDirection = 'improving' | 'worsening' | 'steady';

/** One assertion in a summary or a chat answer. */
export interface Claim {
  text: string;
  source_ref: SourceRef;
  /** Record-grounded severity; absent/null → neutral. */
  severity?: ClaimSeverity | null;
  /** Record-grounded trend direction; absent/null → neutral. */
  trend_direction?: TrendDirection | null;
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

/** Inclusive reference range for a metric, when the source records one. */
export interface ReferenceRange {
  low: number;
  high: number;
}

/** One grounded reading in a per-metric time series. */
export interface ObservationSeriesPoint {
  resource_id: string;
  /** Verbatim recorded value (kept as a string, like SourceRef.value). */
  value: string;
  /** ISO effective time of the reading. */
  timestamp: string;
  /** Abnormal flag from the source ('', 'high', 'vhigh', …); '' is normal. */
  abnormal: string;
}

/**
 * A lazily-fetched, per-metric series for the drill-down trend chart.
 * Independent of the verified-claim contract: each point stays individually
 * grounded (resource_id + value + timestamp). Points are oldest→newest.
 */
export interface ObservationSeries {
  patient_id: number;
  metric: string;
  unit: string;
  reference_range: ReferenceRange | null;
  points: ObservationSeriesPoint[];
}

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
