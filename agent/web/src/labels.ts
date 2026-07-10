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
