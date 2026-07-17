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

/**
 * Which way the latest reading moved vs the prior one — the value's raw motion
 * over time, independent of the reference range. 'up'/'down' when it changed,
 * 'none' when unchanged or there is no prior reading. Drives the UI's movement
 * arrow (↑/↓/—); its colour comes from `trend_direction`. Absent on
 * non-observation claims.
 */
export type ValueDirection = 'up' | 'down' | 'none';

/** One assertion in a summary or a chat answer. */
export interface Claim {
  text: string;
  source_ref: SourceRef;
  /** Record-grounded severity; absent/null → neutral. */
  severity?: ClaimSeverity | null;
  /** Record-grounded trend direction; absent/null → neutral. */
  trend_direction?: TrendDirection | null;
  /** Record-grounded value motion (↑/↓/—); absent/null → no marker. */
  value_direction?: ValueDirection | null;
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
  /**
   * Retrieved guideline snippets backing the answer — a SEPARATE top-level
   * block, deliberately never mixed into the patient-fact `claims` (a
   * guideline recommendation is not a grounded patient observation). Absent
   * or empty → the UI renders no guideline block.
   */
  guideline_evidence?: GuidelineEvidenceItem[];
}

export interface ConversationMessage {
  role: string;
  content: string;
}

// ------------------------------------------------------------- citations

/**
 * Week 2 citation union (see agent/research/week2/02-architecture-spec.md,
 * "Citation"). Claims may now be grounded in three source kinds,
 * discriminated on `source_type`. Week 1 claims arrive as a bare
 * `SourceRef` with no `source_type`; the adapter (src/citations.ts)
 * defaults those to the fhir variant, mirroring the backend deserializer.
 */

/** The Week 1 record citation, explicitly tagged. Fields mirror SourceRef. */
export interface FhirCitation {
  source_type: 'fhir';
  resource_type: string;
  resource_id: string;
  field: string;
  value: string;
  timestamp?: string | null;
}

/** A value extracted from an uploaded document page (OCR-reconciled). */
export interface DocumentCitation {
  source_type: 'document';
  /** source_document_id */
  source_id: string;
  /** page_no of the cited page */
  page_or_section: number;
  /** extracted_fact.id / field_path */
  field_or_chunk_id: string;
  quote_or_value: string;
  /** Normalized [x, y, w, h] on the cited page, each component in 0–1. */
  bbox: number[];
  /** OCR reconciliation confidence, 0–1. */
  confidence: number;
}

/** A retrieved guideline passage — evidence, never a patient record. */
export interface GuidelineCitation {
  source_type: 'guideline';
  /** guideline_document_id */
  source_id: string;
  /** section */
  page_or_section: string;
  /** guideline_chunk.id */
  field_or_chunk_id: string;
  quote_or_value: string;
}

/** Discriminated citation union — what Week 2 claims carry as their source. */
export type Citation = FhirCitation | DocumentCitation | GuidelineCitation;

// ------------------------------------------------------ guideline evidence

/**
 * One retrieved guideline chunk from the separate `guideline_evidence` block
 * on `POST /v1/chat` — mirrors the backend `GuidelineEvidence` model
 * (copilot/rag/retriever.py). Deliberately NOT a `Claim`: guideline backing
 * is literature, never a patient record, and the two surfaces never mix.
 */
export interface GuidelineEvidenceItem {
  source_type: 'guideline';
  /** guideline_chunk row id — locates the exact corpus span. */
  chunk_id: string;
  /** guideline_document row id. */
  document_id: string;
  /** Section label within the guideline. */
  section: string;
  /** The retrieved passage text. */
  content: string;
  /** Retrieval relevance score. */
  score: number;
  /** The typed guideline citation for this chunk; null when unreadable. */
  citation: GuidelineCitation | null;
}

// ---------------------------------------------------------------- documents

/** 202 body from `POST /v1/documents` — ingestion accepted, extraction async. */
export interface DocumentAccepted {
  document_id: string;
  status: string;
  correlation_id: string | null;
}

/**
 * One schema-validated extracted fact from `GET /v1/documents/{id}` with its
 * reconciled page/bbox provenance (mirrors `_fact_body` in
 * copilot/api/routes/documents.py). `supported` is the no-invention gate:
 * true only when the extracted value was actually located in the page's OCR
 * tokens (bbox + match_confidence set). An unsupported fact is surfaced as
 * such — never silently dressed up as grounded.
 */
export interface ExtractedFact {
  /** extracted_fact row id, stringified — the citation join key. */
  id: string;
  /** Schema path of the field, e.g. "hemoglobin" or "medications[0].name". */
  field_path: string;
  /** Extracted value, verbatim. */
  value: string;
  unit: string;
  reference_range: string;
  abnormal_flag: string;
  /** Cited page (1-based); null when reconciliation found no page. */
  page_no: number | null;
  /** Normalized [x, y, w, h] on the cited page; null when not located. */
  bbox: number[] | null;
  /** OCR reconciliation confidence, 0–1; null when not located. */
  match_confidence: number | null;
  supported: boolean;
  /**
   * The document citation the service emitted for this fact (supported facts
   * only), pre-joined on `field_or_chunk_id` by the normalizer. Null when the
   * service emitted none — the fact then renders without a source chip.
   */
  citation: DocumentCitation | null;
}

/**
 * `GET /v1/documents/{document_id}` — ingestion status plus the latest
 * extraction's facts. Polled after upload until `status` is terminal
 * ('extracted' | 'failed').
 */
export interface DocumentDetail {
  document_id: string;
  patient_id: number;
  /** Ingestion status; 'extracted' and 'failed' are terminal. */
  status: string;
  doc_type: string;
  page_count: number | null;
  /** The latest extraction's facts, each carrying its own citation. */
  facts: ExtractedFact[];
}

// ------------------------------------------------------------ write-back

/**
 * The two record kinds a physician can direct-edit. Phase 1 only surfaces
 * `vital` in the UI; `medication` is contract-complete but not yet editable
 * from the drill-down.
 */
export type WriteKind = 'vital' | 'medication';

/**
 * Deterministic verdict from the server-side write-verification pass. For a
 * human direct-edit, an out-of-range value is a SOFT `warning` (surfaced,
 * still confirmable); `blocked` + `errors` is a hard stop (unparseable value,
 * unknown metric, wrong unit) — normally delivered as a 400.
 */
export interface WriteVerdict {
  kind: string;
  metric: string | null;
  blocked: boolean;
  warnings: string[];
  errors: string[];
}

/**
 * The typed vital reading inside a candidate — the only part of the candidate
 * the UI reads (for the echo-back card). The metric/value/unit are server-
 * parsed and authoritative.
 */
export interface WriteVitalCandidate {
  metric: string;
  value: number;
  unit: string;
}

/**
 * The parsed write candidate. Treated as an OPAQUE blob that is round-tripped
 * verbatim into the confirm call — the UI only reads `idempotency_key` (for the
 * confirm URL) and `vital.value`/`vital.unit` (for display). The index
 * signature preserves any server-side fields the client does not model, so
 * confirm re-sends the exact candidate the server proposed.
 */
export interface WriteCandidate {
  kind: string;
  patient_id: { value: number };
  clinician_id: { value: number };
  idempotency_key: string;
  entry_mode: string;
  vital: WriteVitalCandidate | null;
  medication: Record<string, unknown> | null;
  [key: string]: unknown;
}

/** Response to `POST /v1/writes` — the structured echo-back to confirm against. */
export interface ProposedWrite {
  candidate: WriteCandidate;
  verdict: WriteVerdict;
  effective_time: string;
  notice: string;
}

/** Response to `POST /v1/writes/{idempotency_key}/confirm` — the committed record. */
export interface CommittedWrite {
  resource_kind: string;
  new_id: string;
  encounter_id: string | null;
  committed_at: string;
}

/** Request body for `POST /v1/writes`. */
export interface ProposeWriteRequest {
  clinician_id: number;
  patient_id: number;
  kind: WriteKind;
  metric: string;
  raw_value: string;
  unit: string;
}

/** Request body for `POST /v1/writes/{idempotency_key}/confirm`. */
export interface ConfirmWriteRequest {
  clinician_id: number;
  patient_id: number;
  candidate: WriteCandidate;
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

/**
 * Write-back is turned off server-side (503). A distinct type so the UI can
 * show the "editing is disabled" state instead of a generic failure.
 */
export class WriteDisabledError extends ApiError {
  public constructor(message = 'Direct-edit is disabled on this deployment') {
    super(message, 503);
    this.name = 'WriteDisabledError';
  }
}

/**
 * The write was rejected at the parse/verify gate (400): unparseable value,
 * unknown metric, or a unit that does not match the metric. Carries the
 * specific violations so the dialog can surface them verbatim.
 */
export class WriteRejectedError extends ApiError {
  public readonly errors: string[];

  public constructor(errors: string[]) {
    const [first] = errors;
    super(first ?? 'That value could not be recorded', 400);
    this.name = 'WriteRejectedError';
    this.errors = errors.length > 0 ? errors : ['That value could not be recorded'];
  }
}
