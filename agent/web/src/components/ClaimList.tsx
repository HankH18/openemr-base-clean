import type { ReactNode } from 'react';
import type { Claim } from '../api/types';
import { claimTone, type ClaimTone } from '../fmt';
import { humanizeLabel } from '../labels';
import { ProvenanceChip } from './ProvenanceChip';

/**
 * Bold the recorded value inside a text segment, toned by severity. Returns
 * the segment untouched when the value cannot be located within it.
 */
function withBoldedValue(segment: string, value: string, tone: ClaimTone | null): ReactNode {
  const idx = value.length > 0 ? segment.indexOf(value) : -1;
  if (idx === -1) {
    return segment;
  }
  const toneClass = tone !== null ? ` claim-val--${tone}` : '';
  return (
    <>
      {segment.slice(0, idx)}
      <strong className={`claim-val${toneClass}`}>{value}</strong>
      {segment.slice(idx + value.length)}
    </>
  );
}

/**
 * Render a claim as "Label: value …" — a humanized label followed by the
 * remainder with the recorded value bolded and toned by severity. Prose
 * claims (a long or absent leading label) are rendered as-is with the value
 * bolded. Tone is always derived from the ORIGINAL text so coloring is
 * unaffected by the label/rest split.
 */
function claimText(claim: Claim): ReactNode {
  const { text, source_ref } = claim;
  const value = source_ref.value;
  const tone = claimTone(text);

  const sepIdx = text.indexOf(': ');
  if (sepIdx !== -1) {
    const label = text.slice(0, sepIdx);
    const rest = text.slice(sepIdx + 2);
    // A short leading segment is a label; anything longer is prose.
    if (label.trim().split(/\s+/).length <= 4) {
      return (
        <>
          <span className="claim-label">{`${humanizeLabel(label)}: `}</span>
          {withBoldedValue(rest, value, tone)}
        </>
      );
    }
  }

  return withBoldedValue(text, value, tone);
}

export function ClaimList({
  claims,
  dense = false,
}: {
  claims: Claim[];
  dense?: boolean;
}): JSX.Element {
  return (
    <ul className={dense ? 'claims claims--dense' : 'claims'}>
      {claims.map((claim, i) => (
        <li className="claim" key={`${claim.source_ref.resource_id}-${i}`}>
          <span className="claim-text">{claimText(claim)}</span>
          <ProvenanceChip source={claim.source_ref} />
        </li>
      ))}
    </ul>
  );
}
