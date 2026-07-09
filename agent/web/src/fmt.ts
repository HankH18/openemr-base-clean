/** Small formatting helpers — clocks, ages, acuity buckets. */

export function fmtClock(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) {
    return '—';
  }
  const hh = String(date.getHours()).padStart(2, '0');
  const mm = String(date.getMinutes()).padStart(2, '0');
  return `${hh}:${mm}`;
}

export function fmtAge(seconds: number): string {
  if (seconds < 60) {
    return 'just now';
  }
  if (seconds < 3600) {
    return `${Math.round(seconds / 60)} min ago`;
  }
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.round((seconds % 3600) / 60);
  return minutes > 0 ? `${hours} h ${minutes} min ago` : `${hours} h ago`;
}

export type Severity = 'critical' | 'guarded' | 'routine';

/** Acuity buckets. Low acuity is *routine* (neutral ink) — green is reserved for verification. */
export function acuitySeverity(score: number): Severity {
  if (score >= 7.5) {
    return 'critical';
  }
  if (score >= 4) {
    return 'guarded';
  }
  return 'routine';
}

export function fmtAcuity(score: number): string {
  return score.toFixed(1);
}

/** Claims that name an unresolved safety conflict get elevated treatment. */
export function isSafetyClaim(text: string): boolean {
  return text.toLowerCase().includes('conflict');
}

export type ClaimTone = 'critical' | 'high' | 'ok';

/**
 * Tone for the recorded value inside a claim, derived from the claim's own
 * abnormal-flag language. Presentation only — the flag words come from the
 * record synthesis, not from the UI's judgment.
 */
export function claimTone(text: string): ClaimTone | null {
  const t = text.toLowerCase();
  if (t.includes('critical')) {
    return 'critical';
  }
  if (t.includes('— high') || t.includes('- high') || t.includes('trending up')) {
    return 'high';
  }
  if (t.includes('within reference')) {
    return 'ok';
  }
  return null;
}
