/**
 * Per-patient starter questions for the drill-down. Purely a UI
 * affordance — they are ordinary chat messages when sent. Each list is
 * aligned to what the live seed chart can actually answer. Every list ends
 * with "Any MRI report?" — no MRI exists on the live data anywhere, so it
 * demonstrates a withheld, no-source answer.
 */

const DEFAULT_SUGGESTIONS = ['Any MRI report?'];

const BY_PATIENT: Record<number, string[]> = {
  1001: ['Latest hemoglobin?', 'Latest creatinine?', 'Any MRI report?'],
  1002: ['Latest white count?', 'Latest potassium?', 'Any MRI report?'],
  1003: ['Latest glucose?', 'Latest potassium?', "What's his bicarbonate?", 'Any MRI report?'],
  1004: ['Latest lactate?', 'Any MRI report?'],
  1005: ['Latest lipase?', 'Any MRI report?'],
  1006: ['Latest white count?', 'Any MRI report?'],
  1007: ['Latest bicarbonate?', 'Any MRI report?'],
  1008: ['Latest creatinine?', 'Any MRI report?'],
  1009: ['Latest hemoglobin?', 'Any MRI report?'],
  1010: ['Latest aPTT?', 'Any MRI report?'],
  1011: ['Any abnormal labs?', 'Any MRI report?'],
  1012: ['Latest glucose?', 'Any MRI report?'],
  1013: ['Latest potassium?', 'Latest magnesium?', 'Any MRI report?'],
  1014: ['Latest sodium?', 'Any MRI report?'],
  1015: ['Latest troponin?', 'Any MRI report?'],
};

export function suggestionsFor(patientId: number): string[] {
  return BY_PATIENT[patientId] ?? DEFAULT_SUGGESTIONS;
}
