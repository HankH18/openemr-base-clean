import { useState, type CSSProperties } from 'react';
import { Button } from 'react-aria-components';
import type { DocumentAccepted, ObservationSeries, PatientCard } from '../api/types';
import { objectPronoun, type CensusEntry } from '../census';
import { fmtClock, isSafetyClaim } from '../fmt';
import { dedupeMedicationClaims } from '../labels';
import { AcuityMeter } from './AcuityMeter';
import { ClaimList } from './ClaimList';
import { DocumentFacts, type FetchDocumentFn } from './DocumentFacts';
import { DocumentUpload, type UploadFn } from './DocumentUpload';
import type { ConfirmWrite, ProposeWrite } from './EditRecordDialog';
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
  fetchTrend,
  proposeWrite,
  confirmWrite,
  uploadDocument,
  fetchDocument,
}: {
  card: PatientCard;
  entry: CensusEntry | undefined;
  position: number;
  total: number;
  isLast: boolean;
  busy: boolean;
  onDone: () => void;
  fetchTrend?: (metric: string) => Promise<ObservationSeries>;
  /** Physician direct-edit callbacks, bound to this patient at the App seam. */
  proposeWrite?: ProposeWrite;
  confirmWrite?: ConfirmWrite;
  /** Document ingestion, bound to this patient at the App seam. */
  uploadDocument?: UploadFn;
  /**
   * Reader for an uploaded document's extraction status/facts, bound to the
   * clinician at the App seam. When present, an accepted upload's id is
   * carried into the extraction panel (poll → facts → bbox-cited chips).
   */
  fetchDocument?: FetchDocumentFn;
}): JSX.Element {
  // The accepted upload for THIS card. PatientHero remounts per patient
  // (keyed at the App seam), so the panel never leaks across patients.
  const [acceptedDoc, setAcceptedDoc] = useState<DocumentAccepted | null>(null);
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
          <ClaimList
            claims={card.changes_since_last_seen}
            fetchTrend={fetchTrend}
            proposeWrite={proposeWrite}
            confirmWrite={confirmWrite}
          />
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
        <ClaimList
          claims={summaryClaims}
          fetchTrend={fetchTrend}
          proposeWrite={proposeWrite}
          confirmWrite={confirmWrite}
        />
      </section>

      {acceptedDoc !== null && fetchDocument !== undefined ? (
        // Keyed on the document id: a fresh upload remounts the panel, which
        // resets its poll loop cleanly.
        <DocumentFacts
          key={acceptedDoc.document_id}
          documentId={acceptedDoc.document_id}
          fetchDocument={fetchDocument}
        />
      ) : null}

      <div className="hero-actions reveal" style={revealDelay(4)}>
        {uploadDocument !== undefined ? (
          <DocumentUpload
            patientId={card.patient_id}
            upload={uploadDocument}
            onAccepted={setAcceptedDoc}
          />
        ) : null}
        <span className="hero-actions-note">Marks {given} seen; the queue advances by acuity.</span>
        <Button className="btn btn--primary btn--advance" onPress={onDone} isDisabled={busy}>
          {busy ? 'Advancing…' : isLast ? 'Done — finish rounds' : 'Done — next patient'}
        </Button>
      </div>
    </article>
  );
}
