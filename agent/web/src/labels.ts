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
  const spaced = label
    .trim()
    .replace(/_/g, ' ')
    .replace(/([a-z0-9])([A-Z])/g, '$1 $2');
  // Capitalize the FIRST letter of each word, PRESERVE the rest (so
  // acronyms like WBC, BUN stay intact). charAt(0) is used over [0] so the
  // result stays a `string` under noUncheckedIndexedAccess.
  return spaced
    .split(/\s+/)
    .map((w) => (w.length > 0 ? w.charAt(0).toUpperCase() + w.slice(1) : w))
    .join(' ');
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
