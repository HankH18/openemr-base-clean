/**
 * Wire-shape normalizers. The service sometimes serializes ids as
 * `{value: number}` (pydantic domain primitive) and sometimes as a bare
 * number; card payloads may arrive bare or wrapped in `{current, order}`.
 * Everything funnels through here so the rest of the app sees one shape.
 */

import {
  ApiError,
  type AdvanceResult,
  type ChatResponse,
  type Claim,
  type ClaimSeverity,
  type ConversationMessage,
  type DeteriorationAlert,
  type Freshness,
  type ObservationSeries,
  type ObservationSeriesPoint,
  type PatientCard,
  type ReferenceRange,
  type RefreshOutcome,
  type RoundView,
  type SourceRef,
  type TrendDirection,
  type Verification,
  type VerificationAction,
} from './types';

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v);
}

function fail(context: string): never {
  throw new ApiError(`Unexpected response shape from the record service (${context})`);
}

function asString(v: unknown, context: string): string {
  if (typeof v === 'string') {
    return v;
  }
  fail(context);
}

function asNumber(v: unknown, context: string): number {
  if (typeof v === 'number' && Number.isFinite(v)) {
    return v;
  }
  fail(context);
}

/** `patient_id` may serialize as `{value: n}` or `n` — accept both. */
export function normalizeId(v: unknown, context = 'id'): number {
  if (typeof v === 'number' && Number.isFinite(v)) {
    return v;
  }
  if (isRecord(v) && typeof v['value'] === 'number' && Number.isFinite(v['value'])) {
    return v['value'];
  }
  fail(context);
}

function normalizeSourceRef(v: unknown): SourceRef {
  if (!isRecord(v)) {
    fail('source_ref');
  }
  // Timestamp is optional and non-gating — read it tolerantly, never fail on
  // absence. A non-string (or missing) value normalizes to null.
  const timestamp = v['timestamp'];
  return {
    resource_type: asString(v['resource_type'], 'source_ref.resource_type'),
    resource_id: asString(v['resource_id'], 'source_ref.resource_id'),
    field: asString(v['field'], 'source_ref.field'),
    value: asString(v['value'], 'source_ref.value'),
    timestamp: typeof timestamp === 'string' ? timestamp : null,
  };
}

/** Grounded severity, read tolerantly: an unknown/absent value → null (neutral). */
function normalizeSeverity(v: unknown): ClaimSeverity | null {
  return v === 'normal' || v === 'warning' || v === 'critical' ? v : null;
}

/** Grounded trend direction, read tolerantly: unknown/absent → null (neutral). */
function normalizeTrend(v: unknown): TrendDirection | null {
  return v === 'improving' || v === 'worsening' || v === 'steady' ? v : null;
}

export function normalizeClaim(v: unknown): Claim {
  if (!isRecord(v)) {
    fail('claim');
  }
  return {
    text: asString(v['text'], 'claim.text'),
    source_ref: normalizeSourceRef(v['source_ref']),
    severity: normalizeSeverity(v['severity']),
    trend_direction: normalizeTrend(v['trend_direction']),
  };
}

function normalizeClaims(v: unknown, context: string): Claim[] {
  if (!Array.isArray(v)) {
    fail(context);
  }
  return v.map(normalizeClaim);
}

function normalizeFreshness(v: unknown): Freshness {
  if (!isRecord(v)) {
    fail('freshness');
  }
  return {
    as_of: asString(v['as_of'], 'freshness.as_of'),
    age_seconds: asNumber(v['age_seconds'], 'freshness.age_seconds'),
    stale: v['stale'] === true,
  };
}

export function normalizeCard(v: unknown): PatientCard {
  if (!isRecord(v)) {
    fail('patient card');
  }
  return {
    patient_id: normalizeId(v['patient_id'], 'card.patient_id'),
    summary_claims: normalizeClaims(v['summary_claims'], 'card.summary_claims'),
    changes_since_last_seen: normalizeClaims(
      v['changes_since_last_seen'],
      'card.changes_since_last_seen',
    ),
    acuity_score: asNumber(v['acuity_score'], 'card.acuity_score'),
    rank_reason: asString(v['rank_reason'], 'card.rank_reason'),
    freshness: normalizeFreshness(v['freshness']),
  };
}

/** Accepts a bare card, or `{current: card}` with an optional `order`. */
export function normalizeRoundView(v: unknown): RoundView {
  if (isRecord(v) && 'current' in v) {
    const order = Array.isArray(v['order'])
      ? v['order'].map((id) => normalizeId(id, 'order[]'))
      : [];
    return { current: normalizeCard(v['current']), order };
  }
  return { current: normalizeCard(v), order: [] };
}

export function normalizeAdvance(v: unknown): AdvanceResult {
  if (isRecord(v) && v['done'] === true) {
    return { done: true };
  }
  return normalizeRoundView(v);
}

export function normalizeRefresh(v: unknown): RefreshOutcome[] {
  const results = isRecord(v) ? v['results'] : v;
  if (!Array.isArray(results)) {
    fail('refresh.results');
  }
  return results.map((r) => {
    if (!isRecord(r)) {
      fail('refresh.results[]');
    }
    return {
      patient_id: normalizeId(r['patient_id'], 'refresh.patient_id'),
      outcome: asString(r['outcome'], 'refresh.outcome'),
    };
  });
}

export function normalizeAlerts(v: unknown): DeteriorationAlert[] {
  const alerts = isRecord(v) ? v['alerts'] : v;
  if (!Array.isArray(alerts)) {
    fail('alerts');
  }
  return alerts.map((a) => {
    if (!isRecord(a)) {
      fail('alerts[]');
    }
    return {
      patient_id: normalizeId(a['patient_id'], 'alert.patient_id'),
      reason: asString(a['reason'], 'alert.reason'),
    };
  });
}

/**
 * Unknown verification actions degrade, never upgrade: if the service says
 * something we do not recognize, the UI treats the answer as degraded.
 */
function normalizeVerification(v: unknown): Verification {
  if (!isRecord(v)) {
    return { action: 'degraded', passed: false };
  }
  const raw = v['action'];
  const action: VerificationAction =
    raw === 'served' || raw === 'withheld' || raw === 'degraded' ? raw : 'degraded';
  return { action, passed: v['passed'] === true };
}

export function normalizeChat(v: unknown): ChatResponse {
  if (!isRecord(v)) {
    fail('chat response');
  }
  return {
    answer: asString(v['answer'], 'chat.answer'),
    claims: normalizeClaims(v['claims'] ?? [], 'chat.claims'),
    verification: normalizeVerification(v['verification']),
    conversation_id: normalizeId(v['conversation_id'], 'chat.conversation_id'),
    correlation_id: asString(v['correlation_id'], 'chat.correlation_id'),
  };
}

/** Tolerant id read for the series endpoint — defaults to 0 rather than failing. */
function looseId(v: unknown): number {
  if (typeof v === 'number' && Number.isFinite(v)) {
    return v;
  }
  if (isRecord(v) && typeof v['value'] === 'number' && Number.isFinite(v['value'])) {
    return v['value'];
  }
  return 0;
}

function looseString(v: unknown): string {
  return typeof v === 'string' ? v : '';
}

function normalizeReferenceRange(v: unknown): ReferenceRange | null {
  if (!isRecord(v)) {
    return null;
  }
  const low = v['low'];
  const high = v['high'];
  if (
    typeof low === 'number' &&
    Number.isFinite(low) &&
    typeof high === 'number' &&
    Number.isFinite(high)
  ) {
    return { low, high };
  }
  return null;
}

function normalizeObservationPoint(v: unknown): ObservationSeriesPoint | null {
  if (!isRecord(v)) {
    return null;
  }
  const value = looseString(v['value']);
  const timestamp = looseString(v['timestamp']);
  // A point with no value or no time can't be plotted or grounded — drop it.
  if (value === '' || timestamp === '') {
    return null;
  }
  return {
    resource_id: looseString(v['resource_id']),
    value,
    timestamp,
    abnormal: looseString(v['abnormal']),
  };
}

/**
 * Tolerant normalizer for the per-metric trend endpoint. Never throws on a
 * missing field: an unrecognized shape becomes an empty series, and a point
 * that can't be grounded is dropped rather than fabricated.
 */
export function normalizeObservationSeries(v: unknown): ObservationSeries {
  if (!isRecord(v)) {
    return { patient_id: 0, metric: '', unit: '', reference_range: null, points: [] };
  }
  const rawPoints = v['points'];
  const points = Array.isArray(rawPoints)
    ? rawPoints
        .map(normalizeObservationPoint)
        .filter((p): p is ObservationSeriesPoint => p !== null)
    : [];
  return {
    patient_id: looseId(v['patient_id']),
    metric: looseString(v['metric']),
    unit: looseString(v['unit']),
    reference_range: normalizeReferenceRange(v['reference_range']),
    points,
  };
}

export function normalizeConversation(v: unknown): ConversationMessage[] {
  const messages = isRecord(v) ? v['messages'] : v;
  if (!Array.isArray(messages)) {
    fail('conversation.messages');
  }
  return messages.map((m) => {
    if (!isRecord(m)) {
      fail('conversation.messages[]');
    }
    return {
      role: asString(m['role'], 'message.role'),
      content: asString(m['content'], 'message.content'),
    };
  });
}
