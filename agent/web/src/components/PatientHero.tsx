import type { CSSProperties } from 'react';
import { Button } from 'react-aria-components';
import type { PatientCard } from '../api/types';
import { objectPronoun, type CensusEntry } from '../census';
import { fmtClock, isSafetyClaim } from '../fmt';
import { dedupeMedicationClaims } from '../labels';
import { AcuityMeter } from './AcuityMeter';
import { ClaimList } from './ClaimList';
import { FreshnessTag } from './FreshnessTag';
import { ProvenanceChip } from './ProvenanceChip';

function revealDelay(step: number): CSSProperties {
  return { animationDelay: `${step * 70}ms` };
}

/**
 * The chart page for the current patient: identity + acuity band, an
 * elevated safety strip for unresolved conflicts, overnight changes first,
 * then the grounded summary, then the advance control.
 */
export function PatientHero({
  card,
  entry,
  position,
  total,
  isLast,
  busy,
  onDone,
}: {
  card: PatientCard;
  entry: CensusEntry | undefined;
  position: number;
  total: number;
  isLast: boolean;
  busy: boolean;
  onDone: () => void;
}): JSX.Element {
  const name = entry?.name ?? `Patient ${card.patient_id}`;
  const given = entry?.given ?? `patient ${card.patient_id}`;
  const pronoun = objectPronoun(card.patient_id);
  const safetyClaims = card.summary_claims.filter((c) => isSafetyClaim(c.text));
  const summaryClaims = dedupeMedicationClaims(
    card.summary_claims.filter((c) => !isSafetyClaim(c.text)),
  );

  return (
    <article className="hero" aria-label={`Patient card: ${name}`}>
      <header className="hero-head reveal" style={revealDelay(0)}>
        <div className="hero-id">
          <p className="hero-overline">
            <span>
              Patient {position} of {total}
            </span>
            {entry ? <span>Bed {entry.bed}</span> : null}
            <span>MRN {card.patient_id}</span>
          </p>
          <h1 className="hero-name">{name}</h1>
          <p className="hero-line">
            {entry ? `${entry.age} ${entry.sex} · ${entry.service}` : 'Not on the census feed'}
          </p>
          <FreshnessTag freshness={card.freshness} />
        </div>
        <div className="acuity-block">
          <AcuityMeter score={card.acuity_score} />
          <p className="acuity-reason">Ranked here: {card.rank_reason}</p>
        </div>
      </header>

      {safetyClaims.length > 0 ? (
        <div className="safety-strip reveal" style={revealDelay(1)} role="alert">
          <span className="safety-kicker">Safety</span>
          <ul className="claims claims--bare">
            {safetyClaims.map((claim, i) => (
              <li className="claim" key={i}>
                <span className="claim-text">{claim.text}</span>
                <ProvenanceChip source={claim.source_ref} />
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      <section className="section reveal" style={revealDelay(2)}>
        <div className="section-head">
          <h2 className="section-title">Since you last saw {pronoun}</h2>
          <span className="section-count">
            {card.changes_since_last_seen.length}{' '}
            {card.changes_since_last_seen.length === 1 ? 'record' : 'records'}
          </span>
        </div>
        {card.changes_since_last_seen.length > 0 ? (
          <ClaimList claims={card.changes_since_last_seen} />
        ) : (
          <p className="empty-changes">
            No recorded changes since your last review — last synthesis{' '}
            {fmtClock(card.freshness.as_of)}.
          </p>
        )}
      </section>

      <section className="section reveal" style={revealDelay(3)}>
        <div className="section-head">
          <h2 className="section-title">Chart summary</h2>
          <span className="section-count">
            {summaryClaims.length} {summaryClaims.length === 1 ? 'record' : 'records'}
          </span>
        </div>
        <ClaimList claims={summaryClaims} />
      </section>

      <div className="hero-actions reveal" style={revealDelay(4)}>
        <span className="hero-actions-note">Marks {given} seen; the queue advances by acuity.</span>
        <Button className="btn btn--primary btn--advance" onPress={onDone} isDisabled={busy}>
          {busy ? 'Advancing…' : isLast ? 'Done — finish rounds' : 'Done — next patient'}
        </Button>
      </div>
    </article>
  );
}
