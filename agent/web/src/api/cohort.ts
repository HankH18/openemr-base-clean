/**
 * Seed data for the mock adapter — the demo cohort with real clinical
 * content. Values in `source_ref.value` are verbatim record values; claim
 * text quotes them exactly (that is what the verification gate checks).
 *
 * Patient 1005 has two phases: a quiet baseline, and a deterioration state
 * (critical lactate) that the mock fires mid-round to exercise the
 * proactive-alert flow.
 */

import type { Claim, ObservationSeries, ObservationSeriesPoint, ReferenceRange } from './types';

export interface ChatFact {
  /** Lowercased keywords; any hit answers with this fact. */
  topics: string[];
  action: 'served' | 'degraded';
  answer: string;
  claims: Claim[];
  /** Only answerable once the deterioration state has fired (1005 lactate). */
  requiresDeterioration?: boolean;
  /** Grounded answer used before the deterioration state fires. If absent, the topic falls through to the withheld default. */
  answerBefore?: string;
  /** Claims backing `answerBefore`. */
  claimsBefore?: Claim[];
}

export interface CohortPhase {
  acuity: number;
  rankReason: string;
  ageSeconds: number;
  stale: boolean;
  summary: Claim[];
  changes: Claim[];
}

export interface CohortPatient {
  id: number;
  base: CohortPhase;
  deteriorated?: CohortPhase;
  facts: ChatFact[];
  /**
   * Per-metric trend series, keyed by the humanized metric label the
   * drill-down derives from an Observation claim ("Troponin I", "Lactate", …).
   * Lets the trend chart demo offline. Mirrors the shape/direction of the
   * live seed's serial readings.
   */
  series?: Record<string, ObservationSeries>;
}

function claim(
  text: string,
  resourceType: string,
  resourceId: string,
  field: string,
  value: string,
): Claim {
  return {
    text,
    source_ref: { resource_type: resourceType, resource_id: resourceId, field, value },
  };
}

// ---- Trend series (oldest→newest) -----------------------------------------
// A single wall-clock anchor so timestamps are stable for the session; each
// reading is placed a fixed number of hours before it.
const SERIES_NOW = Date.now();

function hoursAgo(h: number): string {
  return new Date(SERIES_NOW - h * 3_600_000).toISOString();
}

function reading(
  resourceId: string,
  value: string,
  hAgo: number,
  abnormal: string,
): ObservationSeriesPoint {
  return { resource_id: resourceId, value, timestamp: hoursAgo(hAgo), abnormal };
}

function series(
  patientId: number,
  metric: string,
  unit: string,
  range: ReferenceRange | null,
  points: ObservationSeriesPoint[],
): ObservationSeries {
  return { patient_id: patientId, metric, unit, reference_range: range, points };
}

// 1001 · rising troponin (NSTEMI) — last reading is the cited claim value 0.9.
const TROP_SERIES_1001 = series(1001, 'Troponin I', 'ng/mL', { low: 0, high: 0.04 }, [
  reading('trop-1001-a', '0.02', 22, ''),
  reading('trop-1001-b', '0.05', 12, 'high'),
  reading('trop-1001-c', '0.42', 6, 'vhigh'),
  reading('trop-1001', '0.9', 1, 'vhigh'),
]);

// 1002 · potassium climbing onto the ACE inhibitor — ends at the cited 5.6.
const K_SERIES_1002 = series(1002, 'Potassium', 'mmol/L', { low: 3.5, high: 5.1 }, [
  reading('k-1002-a', '4.6', 20, ''),
  reading('k-1002-b', '5.1', 8, ''),
  reading('k-1002', '5.6', 1, 'high'),
]);

// 1003 · hemoglobin holding within range — a calm, all-normal trend.
const HGB_SERIES_1003 = series(1003, 'Hemoglobin', 'g/dL', { low: 13.0, high: 17.0 }, [
  reading('hgb-1003-a', '13.9', 30, ''),
  reading('hgb-1003-b', '13.7', 14, ''),
  reading('hgb-1003', '13.5', 2, ''),
]);

// 1004 · sodium stable and normal — ends at the cited 140.
const NA_SERIES_1004 = series(1004, 'Sodium', 'mmol/L', { low: 135, high: 145 }, [
  reading('na-1004-a', '138', 28, ''),
  reading('na-1004-b', '139', 12, ''),
  reading('na-1004', '140', 2, ''),
]);

// 1005 · serial lactate rising to critical — mirrors the live 1.9→2.8→4.2
// trend, extended to the deteriorated card's cited 5.0.
const LACT_SERIES_1005 = series(1005, 'Lactate', 'mmol/L', { low: 0.5, high: 2.0 }, [
  reading('lact-1005-a', '1.9', 20, ''),
  reading('lact-1005-b', '2.8', 10, 'high'),
  reading('lact-1005-c', '4.2', 4, 'vhigh'),
  reading('lact-1005', '5.0', 1, 'vhigh'),
]);

// 1005 · heart rate climbing over the last four hours — ends at the cited 118.
const HR_SERIES_1005 = series(1005, 'Heart rate', 'bpm', { low: 60, high: 100 }, [
  reading('hr-1005-a', '92', 4, ''),
  reading('hr-1005-b', '104', 2, 'high'),
  reading('hr-1005', '118', 1, 'high'),
]);

// ---- 1001 · Ernest Vaughn · NSTEMI ----------------------------------------

const TROP_1001 = claim(
  'Troponin I 0.9 ng/mL — critical high (reference 0.00–0.04).',
  'Observation', 'trop-1001', 'valueQuantity.value', '0.9',
);
const TROP_DELTA_1001 = claim(
  'Troponin I rose 0.4 → 0.9 ng/mL overnight; 04:12 draw.',
  'Observation', 'trop-1001', 'valueQuantity.value', '0.9',
);
const ASA_1001 = claim(
  'Active order: aspirin 81 mg daily, oral; last given 06:00.',
  'MedicationRequest', 'med-asa-1001', 'medicationCodeableConcept.text', 'aspirin',
);
const HEP_1001 = claim(
  'New overnight: heparin infusion started 05:30 per ACS protocol.',
  'MedicationRequest', 'med-hep-1001', 'medicationCodeableConcept.text', 'heparin',
);
const DX_1001 = claim(
  'Admitted for chest pain; working diagnosis NSTEMI.',
  'Condition', 'cond-1001', 'code.text', 'NSTEMI',
);
const ECG_1001 = claim(
  'ECG 03:50 report on file: ST depression V4–V6.',
  'DiagnosticReport', 'ecg-1001', 'conclusion', 'ST depression V4-V6',
);

// ---- 1002 · Rosa Delgado · hyperkalemia -----------------------------------

const K_1002 = claim(
  'Potassium 5.6 mmol/L — high (reference 3.5–5.1).',
  'Observation', 'k-1002', 'valueQuantity.value', '5.6',
);
const K_DELTA_1002 = claim(
  'Potassium trended 5.1 → 5.6 mmol/L since yesterday 18:30.',
  'Observation', 'k-1002', 'valueQuantity.value', '5.6',
);
const LIS_1002 = claim(
  'Active order: lisinopril 20 mg daily — ACE inhibitor in the setting of rising potassium.',
  'MedicationRequest', 'med-lis-1002', 'medicationCodeableConcept.text', 'lisinopril',
);

// ---- 1003 · Marcus Webb · drug–allergy conflict ---------------------------

const ALLERGY_1003 = claim(
  'Documented penicillin allergy conflicts with the active amoxicillin order.',
  'AllergyIntolerance', 'allergy-1003', 'code.text', 'penicillin',
);
const AMOX_1003 = claim(
  'Active order: amoxicillin 500 mg three times daily, oral.',
  'MedicationRequest', 'med-amox-1003', 'medicationCodeableConcept.text', 'amoxicillin',
);
const HGB_1003 = claim(
  'Hemoglobin 13.5 g/dL — within reference (13.0–17.0).',
  'Observation', 'hgb-1003', 'valueQuantity.value', '13.5',
);

// ---- 1004 · June Okafor · stable ------------------------------------------

const NA_1004 = claim(
  'Sodium 140 mmol/L — within reference (135–145).',
  'Observation', 'na-1004', 'valueQuantity.value', '140',
);
const OMEP_1004 = claim(
  'Active order: omeprazole 20 mg daily, oral.',
  'MedicationRequest', 'med-omep-1004', 'medicationCodeableConcept.text', 'omeprazole',
);

// ---- 1005 · Lillian Cho · sepsis (deteriorates mid-round) -----------------

const IVF_1005 = claim(
  "IV fluid resuscitation running: lactated Ringer's at 125 mL/h.",
  'MedicationRequest', 'med-ivf-1005', 'medicationCodeableConcept.text', "lactated Ringer's",
);
const BCX_1005 = claim(
  'Blood cultures drawn 22:40 — status pending.',
  'Observation', 'bcx-1005', 'status', 'pending',
);
const LACT_1005 = claim(
  'Lactate 5.0 mmol/L — critical high (reference 0.5–2.0); resulted 06:58.',
  'Observation', 'lact-1005', 'valueQuantity.value', '5.0',
);
const HR_1005 = claim(
  'Heart rate trending up, 92 → 118 bpm over the last four hours.',
  'Observation', 'hr-1005', 'valueQuantity.value', '118',
);

export const ALERT_REASON_1005 =
  'New lactate 5.0 mmol/L — critical high (reference 0.5–2.0), resulted 06:58. Acuity 4.2 → 9.3.';

export const COHORT: CohortPatient[] = [
  {
    id: 1001,
    series: { 'Troponin I': TROP_SERIES_1001 },
    base: {
      acuity: 9.1,
      rankReason: 'critical troponin',
      ageSeconds: 260,
      stale: false,
      summary: [TROP_1001, ASA_1001, DX_1001],
      changes: [TROP_DELTA_1001, HEP_1001],
    },
    facts: [
      {
        topics: ['troponin', 'trop'],
        action: 'served',
        answer:
          'Troponin I is 0.9 ng/mL, resulted from the 04:12 draw — critical high against a reference of 0.00–0.04, up from 0.4 at 22:00.',
        claims: [TROP_1001],
      },
      {
        topics: ['aspirin', 'asa'],
        action: 'served',
        answer: 'Yes — aspirin 81 mg daily is active. The 06:00 dose was given.',
        claims: [ASA_1001],
      },
      {
        topics: ['heparin'],
        action: 'served',
        answer: 'A heparin infusion was started at 05:30 per ACS protocol; it is running now.',
        claims: [HEP_1001],
      },
      {
        topics: ['ecg', 'ekg'],
        action: 'degraded',
        answer:
          'The 03:50 ECG report on file reads "ST depression V4–V6" — but the live re-check of that report did not complete, so treat this as last-known, not confirmed.',
        claims: [ECG_1001],
      },
    ],
  },
  {
    id: 1002,
    series: { Potassium: K_SERIES_1002 },
    base: {
      acuity: 5.4,
      rankReason: 'potassium rising on ACE inhibitor',
      ageSeconds: 540,
      stale: false,
      summary: [K_1002, LIS_1002],
      changes: [K_DELTA_1002],
    },
    facts: [
      {
        topics: ['potassium', 'k+', 'hyperkalemia'],
        action: 'served',
        answer:
          'Potassium is 5.6 mmol/L — high against a reference of 3.5–5.1, and trending up from 5.1 yesterday evening.',
        claims: [K_1002],
      },
      {
        topics: ['lisinopril', 'ace'],
        action: 'served',
        answer:
          'Lisinopril 20 mg daily is still an active order. Flagging: it is an ACE inhibitor and her potassium is trending up.',
        claims: [LIS_1002, K_1002],
      },
    ],
  },
  {
    id: 1003,
    series: { Hemoglobin: HGB_SERIES_1003 },
    base: {
      acuity: 2.3,
      rankReason: 'drug–allergy conflict flagged',
      ageSeconds: 780,
      stale: false,
      summary: [ALLERGY_1003, AMOX_1003, HGB_1003],
      changes: [],
    },
    facts: [
      {
        topics: ['allergy', 'allergies', 'penicillin', 'amoxicillin'],
        action: 'served',
        answer:
          'The chart documents a penicillin allergy, and amoxicillin 500 mg three times daily is an active order — a direct drug–allergy conflict. Both records are cited below.',
        claims: [ALLERGY_1003, AMOX_1003],
      },
      {
        topics: ['hemoglobin', 'hgb', 'cbc'],
        action: 'served',
        answer: 'Hemoglobin is 13.5 g/dL — within the 13.0–17.0 reference range.',
        claims: [HGB_1003],
      },
    ],
  },
  {
    id: 1004,
    series: { Sodium: NA_SERIES_1004 },
    base: {
      acuity: 1.2,
      rankReason: 'all results within reference',
      ageSeconds: 9500,
      stale: true,
      summary: [NA_1004, OMEP_1004],
      changes: [],
    },
    facts: [
      {
        topics: ['sodium', 'na'],
        action: 'served',
        answer: 'Sodium is 140 mmol/L — within the 135–145 reference range.',
        claims: [NA_1004],
      },
      {
        topics: ['omeprazole', 'ppi'],
        action: 'served',
        answer: 'Omeprazole 20 mg daily is active; no changes to the order overnight.',
        claims: [OMEP_1004],
      },
    ],
  },
  {
    id: 1005,
    series: { Lactate: LACT_SERIES_1005, 'Heart rate': HR_SERIES_1005 },
    base: {
      acuity: 4.2,
      rankReason: 'infection under treatment; lactate pending',
      ageSeconds: 420,
      stale: false,
      summary: [IVF_1005, BCX_1005],
      changes: [],
    },
    deteriorated: {
      acuity: 9.3,
      rankReason: 'critical lactate — deteriorating',
      ageSeconds: 40,
      stale: false,
      summary: [LACT_1005, HR_1005, IVF_1005, BCX_1005],
      changes: [LACT_1005, HR_1005],
    },
    facts: [
      {
        topics: ['lactate'],
        action: 'served',
        answer:
          'Lactate resulted 5.0 mmol/L at 06:58 — critical high against a reference of 0.5–2.0. This is the finding that raised her acuity.',
        answerBefore:
          'A lactate has been ordered but has not resulted yet; blood cultures from 22:40 are also still pending. I can only cite what is on file.',
        claimsBefore: [BCX_1005],
        claims: [LACT_1005],
        requiresDeterioration: true,
      },
      {
        topics: ['fluid', 'fluids', 'ringer'],
        action: 'served',
        answer: "Lactated Ringer's is running at 125 mL/h for fluid resuscitation.",
        claims: [IVF_1005],
      },
      {
        topics: ['culture', 'cultures'],
        action: 'served',
        answer: 'Blood cultures were drawn at 22:40 and are still pending — no growth reported yet.',
        claims: [BCX_1005],
      },
      {
        topics: ['heart rate', 'tachycard', 'vitals'],
        action: 'served',
        answer: 'Heart rate has climbed from 92 to 118 bpm over the last four hours.',
        claims: [HR_1005],
        requiresDeterioration: true,
      },
    ],
  },
];
