/**
 * Human-readable display labels for the clinical ledger.
 *
 * Backend claim labels arrive in whatever casing the source used —
 * raw FHIR resource types ("MedicationRequest"), snake_case observation
 * names ("oxygen_saturation"), or already-tidy prose ("Heart rate").
 * `humanizeLabel` normalizes all of them to a single doctor-facing form
 * without mangling acronyms (WBC, BUN stay intact).
 */

import type { Claim } from './api/types';

const RESOURCE_LABELS: Record<string, string> = {
  MedicationRequest: 'Medication',
  Condition: 'Condition',
  AllergyIntolerance: 'Allergy',
  Observation: 'Observation',
  Immunization: 'Immunization',
  Procedure: 'Procedure',
  DiagnosticReport: 'Diagnostic report',
};

export function humanizeLabel(label: string): string {
  const mapped = RESOURCE_LABELS[label.trim()];
  if (mapped) {
    return mapped;
  }
  // Split snake_case into words and title-case ordinary lowercase words. A word
  // with an internal uppercase letter is an acronym / mixed-case abbreviation
  // (WBC, BUN, aPTT) and is left verbatim. charAt(0) over [0] keeps the result a
  // `string` under noUncheckedIndexedAccess.
  return label
    .trim()
    .replace(/_/g, ' ')
    .split(/\s+/)
    .map((w) => {
      if (w.length === 0) {
        return w;
      }
      return /[A-Z]/.test(w.slice(1)) ? w : w.charAt(0).toUpperCase() + w.slice(1);
    })
    .join(' ');
}

/**
 * The metric name an Observation claim is about — the leading run of words
 * before the first token carrying a digit or a dash separator. "Troponin I
 * 0.9 ng/mL — critical high" → "Troponin I"; "Potassium 5.6 mmol/L" →
 * "Potassium". This is the humanized label the trend endpoint groups by.
 * Falls back to the humanized resource type if no leading label is found.
 */
export function observationMetric(claim: Claim): string {
  const words = claim.text.trim().split(/\s+/);
  const out: string[] = [];
  for (const w of words) {
    if (/\d/.test(w) || /^[—–-]$/.test(w)) {
      break;
    }
    out.push(w);
  }
  const label = out.join(' ').replace(/[.,:;]+$/, '').trim();
  return label.length > 0 ? label : humanizeLabel(claim.source_ref.resource_type);
}

/**
 * True when a claim is a numeric Observation — the only kind a trend chart
 * makes sense for. A status/text Observation (e.g. cultures "pending") is
 * excluded, so the "View trend" affordance never appears where it can't plot.
 */
export function isNumericObservation(claim: Claim): boolean {
  if (claim.source_ref.resource_type !== 'Observation') {
    return false;
  }
  const trimmed = claim.source_ref.value.trim();
  return trimmed !== '' && Number.isFinite(Number(trimmed));
}

// ---------------------------------------------------------- writable vitals

/**
 * The closed set of vitals a physician may direct-edit in Phase 1. Mirrors the
 * backend `WritableMetric` StrEnum exactly — the value sent as the `metric`
 * field of a propose request. Only vitals are editable in Phase 1.
 */
export type WritableMetric =
  | 'heart_rate'
  | 'spo2'
  | 'systolic_bp'
  | 'diastolic_bp'
  | 'respiratory_rate'
  | 'temperature'
  | 'weight'
  | 'height';

export interface WritableMetricSpec {
  metric: WritableMetric;
  /** Fixed doctor-facing label shown in the edit dialog (never editable). */
  label: string;
  /** The unit sent with the write and shown LOCKED — never a free-text field. */
  unit: string;
  /**
   * Absolute physiologic plausibility bounds. A value outside is a SOFT
   * warning for a human direct-edit — surfaced, still confirmable. Used by the
   * mock adapter to demo the amber banner offline; the live gate lives server-side.
   */
  min: number;
  max: number;
  /** Lowercased label forms that identify this metric in an Observation claim. */
  aliases: string[];
}

/** The write-metric registry — the single source of label/unit/bounds/aliases. */
export const WRITABLE_METRICS: Record<WritableMetric, WritableMetricSpec> = {
  heart_rate: {
    metric: 'heart_rate',
    label: 'Heart rate',
    unit: 'bpm',
    min: 20,
    max: 220,
    aliases: ['heart rate', 'pulse'],
  },
  spo2: {
    metric: 'spo2',
    label: 'Oxygen saturation',
    unit: '%',
    min: 50,
    max: 100,
    aliases: ['oxygen saturation', 'spo2', 'spo₂', 'o2 saturation'],
  },
  systolic_bp: {
    metric: 'systolic_bp',
    label: 'Systolic blood pressure',
    unit: 'mmHg',
    min: 50,
    max: 260,
    aliases: ['systolic blood pressure', 'systolic bp', 'systolic'],
  },
  diastolic_bp: {
    metric: 'diastolic_bp',
    label: 'Diastolic blood pressure',
    unit: 'mmHg',
    min: 20,
    max: 160,
    aliases: ['diastolic blood pressure', 'diastolic bp', 'diastolic'],
  },
  respiratory_rate: {
    metric: 'respiratory_rate',
    label: 'Respiratory rate',
    unit: 'breaths/min',
    min: 4,
    max: 60,
    aliases: ['respiratory rate', 'respiration'],
  },
  temperature: {
    metric: 'temperature',
    label: 'Temperature',
    unit: '°F',
    min: 90,
    max: 113,
    aliases: ['temperature', 'temp'],
  },
  weight: {
    metric: 'weight',
    label: 'Weight',
    unit: 'lb',
    min: 1,
    max: 1000,
    aliases: ['weight'],
  },
  height: {
    metric: 'height',
    label: 'Height',
    unit: 'in',
    min: 10,
    max: 100,
    aliases: ['height'],
  },
};

/**
 * The `WritableMetric` a claim is editable as, or null. Only a numeric
 * Observation whose leading metric label matches a writable-vital alias
 * qualifies — labs (Troponin, Potassium, …) and non-observations never do, so
 * the "Edit" affordance appears exactly where a new vital record can be written.
 * The alias is matched at a word boundary so "Heart rate trending up, 92 → 118…"
 * still resolves to `heart_rate`.
 */
export function writableMetric(claim: Claim): WritableMetric | null {
  if (claim.source_ref.resource_type !== 'Observation') {
    return null;
  }
  if (!isNumericObservation(claim)) {
    return null;
  }
  const label = observationMetric(claim).trim().toLowerCase();
  if (label === '') {
    return null;
  }
  for (const spec of Object.values(WRITABLE_METRICS)) {
    for (const alias of spec.aliases) {
      if (label === alias || label.startsWith(`${alias} `)) {
        return spec.metric;
      }
    }
  }
  return null;
}

/** Trim, lowercase, and drop trailing dots so medication values compare cleanly. */
function normalizeMedicationValue(value: string): string {
  return value.trim().toLowerCase().replace(/\.+$/, '').trim();
}

/**
 * Collapse duplicate medication rows. The summary sometimes lists the same
 * drug twice — a bare name ("Hydromorphone.") and a full sig ("Hydromorphone
 * 0.5 mg IV q4h PRN pain."). Drop any MedicationRequest claim whose value is a
 * strict prefix of another MedicationRequest claim's value, keeping the
 * longer/more-informative one. Non-medication claims are never dropped and
 * original order is preserved.
 */
export function dedupeMedicationClaims(claims: Claim[]): Claim[] {
  const medicationValues = claims
    .filter((c) => c.source_ref.resource_type === 'MedicationRequest')
    .map((c) => normalizeMedicationValue(c.source_ref.value));

  return claims.filter((claim) => {
    if (claim.source_ref.resource_type !== 'MedicationRequest') {
      return true;
    }
    const value = normalizeMedicationValue(claim.source_ref.value);
    if (value.length === 0) {
      return true;
    }
    const isPrefixOfAnother = medicationValues.some(
      (other) => other.length > value.length && other.startsWith(value),
    );
    return !isPrefixOfAnother;
  });
}
