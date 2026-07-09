import type { ReactNode } from 'react';
import type { Claim } from '../api/types';
import { claimTone } from '../fmt';
import { ProvenanceChip } from './ProvenanceChip';

/** Sets the recorded value inside the claim text in data type, toned by severity. */
function claimText(claim: Claim): ReactNode {
  const { text, source_ref } = claim;
  const value = source_ref.value;
  const idx = value.length > 0 ? text.indexOf(value) : -1;
  if (idx === -1) {
    return text;
  }
  const tone = claimTone(text);
  const toneClass = tone !== null ? ` claim-val--${tone}` : '';
  return (
    <>
      {text.slice(0, idx)}
      <strong className={`claim-val${toneClass}`}>{value}</strong>
      {text.slice(idx + value.length)}
    </>
  );
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
