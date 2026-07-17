/**
 * Post-upload extraction panel — the data seam that makes the bbox overlay
 * real. After `POST /v1/documents` is accepted, this polls
 * `GET /v1/documents/{id}` until the ingestion status is terminal, then
 * renders the extracted facts as ledger rows. Every supported fact carries a
 * document-variant `ProvenanceChip` built from its wire citation
 * (source_id + page + bbox), so pressing it opens the scanned page with the
 * cited region boxed (`DocumentEvidence` → `EvidenceOverlay`).
 *
 * Honest states, all of them:
 *
 *   pending   — extraction is async and takes seconds; a quiet progress row
 *   failed    — the pipeline reported `status: 'failed'`
 *   ready, 0  — extraction finished but read nothing; said plainly
 *   stalled   — the poll cap was reached while still non-terminal
 *   error     — the status could not be fetched: a definitive 4xx stops the
 *               poll immediately; transient failures retry until the cap
 *
 * Per-fact honesty: a fact with `supported: false` (the no-invention gate
 * could not locate its value in the page's OCR tokens) is dimmed and wears an
 * amber "not found on page" tag; a cited fact without usable geometry keeps
 * its chip but degrades to the text-only popover — never a broken overlay.
 */

import { useEffect, useRef, useState } from 'react';
import { ApiError, type DocumentDetail, type ExtractedFact } from '../api/types';
import { humanizeLabel } from '../labels';
import { ProvenanceChip } from './ProvenanceChip';

/** Re-check interval — extraction takes seconds; do not hammer the service. */
export const DOCUMENT_POLL_INTERVAL_MS = 2500;
/** Poll cap — after this many checks the panel stops and says so honestly. */
export const DOCUMENT_POLL_LIMIT = 24;

export type FetchDocumentFn = (documentId: string) => Promise<DocumentDetail>;

type Phase =
  | { phase: 'pending'; status: string }
  | { phase: 'ready'; detail: DocumentDetail }
  | { phase: 'failed' }
  | { phase: 'stalled' }
  | { phase: 'error'; message: string };

/**
 * A definitive refusal (4xx: unauthorized, forbidden, gone) will not change
 * on retry — stop immediately instead of hammering the endpoint. Network
 * failures and 5xx are treated as transient and re-polled until the cap.
 */
function definitiveRefusal(error: unknown): string | null {
  if (error instanceof ApiError && error.status !== null && error.status < 500) {
    return `Could not read the extraction — the record service replied ${error.status}.`;
  }
  return null;
}

/**
 * Doctor-facing label for a schema field path: "hemoglobin" → "Hemoglobin",
 * "medications[0].name" → "Medications 1" (indices become 1-based ordinals,
 * a trailing ".name" segment is redundant next to the value and dropped).
 */
function factLabel(fieldPath: string): string {
  const spaced = fieldPath
    .replace(/\[(\d+)\]/g, (_match, index: string) => ` ${Number(index) + 1}`)
    .replace(/\./g, ' ')
    .replace(/\s+name$/i, '');
  return humanizeLabel(spaced);
}

function FactRow({ fact }: { fact: ExtractedFact }): JSX.Element {
  return (
    <li className={fact.supported ? 'claim' : 'claim claim--unsupported'}>
      <span className="claim-text">
        <span className="claim-label">{factLabel(fact.field_path)}:</span>{' '}
        <span className="claim-val">
          {fact.value}
          {fact.unit !== '' ? ` ${fact.unit}` : ''}
        </span>
        {fact.abnormal_flag !== '' ? (
          <span className="doc-fact-flag"> {fact.abnormal_flag}</span>
        ) : null}
        {fact.reference_range !== '' ? (
          <span className="doc-fact-range"> · ref {fact.reference_range}</span>
        ) : null}
      </span>
      <span className="claim-tools">
        {!fact.supported ? (
          <span
            className="doc-fact-unsupported"
            title="The extractor suggested this value, but it could not be located on the scanned page — treat it as unverified."
          >
            Not found on page
          </span>
        ) : null}
        {fact.citation !== null ? <ProvenanceChip source={fact.citation} /> : null}
      </span>
    </li>
  );
}

export function DocumentFacts({
  documentId,
  fetchDocument,
  pollIntervalMs = DOCUMENT_POLL_INTERVAL_MS,
  maxPolls = DOCUMENT_POLL_LIMIT,
}: {
  /** The accepted upload's id (from the 202 envelope). */
  documentId: string;
  /** Injected reader — the App seam binds `api.getDocument` to the clinician. */
  fetchDocument: FetchDocumentFn;
  /** Injectable for tests; defaults are the product cadence. */
  pollIntervalMs?: number;
  maxPolls?: number;
}): JSX.Element {
  const [state, setState] = useState<Phase>({ phase: 'pending', status: '' });

  // The fetcher is read through a ref so an unstable prop identity (an inline
  // arrow at the App seam) can never restart the poll loop mid-flight.
  const fetchRef = useRef(fetchDocument);
  useEffect(() => {
    fetchRef.current = fetchDocument;
  }, [fetchDocument]);

  useEffect(() => {
    let cancelled = false;
    let timer: number | null = null;
    setState({ phase: 'pending', status: '' });

    const tick = async (attempt: number): Promise<void> => {
      let detail: DocumentDetail | null = null;
      try {
        detail = await fetchRef.current(documentId);
      } catch (error: unknown) {
        if (cancelled) {
          return;
        }
        const refusal = definitiveRefusal(error);
        if (refusal !== null) {
          setState({ phase: 'error', message: refusal });
          return;
        }
        // Transient transport failure — keep polling until the cap.
        detail = null;
      }
      if (cancelled) {
        return;
      }
      if (detail !== null) {
        if (detail.status === 'extracted') {
          setState({ phase: 'ready', detail });
          return;
        }
        if (detail.status === 'failed') {
          setState({ phase: 'failed' });
          return;
        }
        setState({ phase: 'pending', status: detail.status });
      }
      if (attempt + 1 >= maxPolls) {
        setState(
          detail === null
            ? {
                phase: 'error',
                message: 'Could not check extraction status — the record service is unreachable.',
              }
            : { phase: 'stalled' },
        );
        return;
      }
      timer = window.setTimeout(() => {
        void tick(attempt + 1);
      }, pollIntervalMs);
    };

    void tick(0);
    return () => {
      cancelled = true;
      if (timer !== null) {
        window.clearTimeout(timer);
      }
    };
  }, [documentId, pollIntervalMs, maxPolls]);

  const facts = state.phase === 'ready' ? state.detail.facts : [];

  return (
    <section className="section doc-extract reveal" aria-label="Uploaded document extraction">
      <div className="section-head">
        <h2 className="section-title">From the uploaded document</h2>
        {state.phase === 'ready' ? (
          <span className="section-count">
            {facts.length} {facts.length === 1 ? 'fact' : 'facts'}
          </span>
        ) : null}
      </div>

      {state.phase === 'pending' ? (
        <p className="doc-extract-wait" role="status">
          Extracting — reading the scanned pages. Each value is checked against the page before
          it is shown.
        </p>
      ) : null}

      {state.phase === 'failed' ? (
        <p className="doc-extract-error" role="alert">
          Extraction failed — nothing could be read from this document. No facts were invented in
          its place.
        </p>
      ) : null}

      {state.phase === 'stalled' ? (
        <p className="doc-extract-wait" role="status">
          Extraction is taking longer than expected — stopped checking for now. The document is
          safely ingested; its facts will be readable once processing finishes.
        </p>
      ) : null}

      {state.phase === 'error' ? (
        <p className="doc-extract-error" role="alert">
          {state.message}
        </p>
      ) : null}

      {state.phase === 'ready' ? (
        facts.length > 0 ? (
          <ul className="claims doc-facts">
            {facts.map((fact, index) => (
              <FactRow key={fact.id !== '' ? fact.id : `fact-${index}`} fact={fact} />
            ))}
          </ul>
        ) : (
          <p className="empty-changes">
            Extraction finished, but no facts could be read from this document.
          </p>
        )
      ) : null}
    </section>
  );
}
