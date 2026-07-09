/**
 * Per-patient starter questions for the drill-down. Purely a UI
 * affordance — they are ordinary chat messages when sent. The last entry
 * for 1001 (ECG) demonstrates a degraded verification; "MRI report?" has
 * no source anywhere and demonstrates a withheld answer.
 */

const DEFAULT_SUGGESTIONS = ['Any MRI report?'];

const BY_PATIENT: Record<number, string[]> = {
  1001: ['Latest troponin?', 'Is he on aspirin?', 'What did the ECG show?', 'Any MRI report?'],
  1002: ['Latest potassium?', 'Is lisinopril still active?', 'Any MRI report?'],
  1003: ['What conflicts with his allergy?', 'Latest hemoglobin?', 'Any MRI report?'],
  1004: ['Latest sodium?', 'Is omeprazole still active?', 'Any MRI report?'],
  1005: ['Latest lactate?', 'Culture results?', 'What fluids are running?', 'Any MRI report?'],
};

export function suggestionsFor(patientId: number): string[] {
  return BY_PATIENT[patientId] ?? DEFAULT_SUGGESTIONS;
}
