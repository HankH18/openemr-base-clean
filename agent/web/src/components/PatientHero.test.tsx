import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import type {
  DocumentAccepted,
  DocumentDetail,
  ExtractedFact,
  PatientCard,
} from '../api/types';
import type { DocType } from '../api/documents';
import { PatientHero } from './PatientHero';

const CARD: PatientCard = {
  patient_id: 1003,
  summary_claims: [],
  changes_since_last_seen: [],
  acuity_score: 7.5,
  rank_reason: 'DKA on insulin infusion',
  freshness: { as_of: '2026-07-16T06:30:00Z', age_seconds: 60, stale: false },
};

const ACCEPTED: DocumentAccepted = {
  document_id: 'doc-42',
  status: 'processing',
  correlation_id: 'corr-1',
};

const FACT: ExtractedFact = {
  id: '7',
  field_path: 'hemoglobin',
  value: '9.1',
  unit: 'g/dL',
  reference_range: '',
  abnormal_flag: '',
  page_no: 3,
  bbox: [0.25, 0.5, 0.5, 0.25],
  match_confidence: 0.92,
  supported: true,
  citation: {
    source_type: 'document',
    source_id: 'doc-42',
    page_or_section: 3,
    field_or_chunk_id: '7',
    quote_or_value: '9.1',
    bbox: [0.25, 0.5, 0.5, 0.25],
    confidence: 0.92,
  },
};

const DETAIL: DocumentDetail = {
  document_id: 'doc-42',
  patient_id: 1003,
  status: 'extracted',
  doc_type: 'lab_pdf',
  page_count: 3,
  facts: [FACT],
};

function renderHero(overrides: {
  uploadDocument?: (file: File, docType: DocType) => Promise<DocumentAccepted>;
  fetchDocument?: (documentId: string) => Promise<DocumentDetail>;
}): ReturnType<typeof render> {
  return render(
    <PatientHero
      card={CARD}
      entry={undefined}
      position={1}
      total={1}
      isLast
      busy={false}
      onDone={() => undefined}
      {...overrides}
    />,
  );
}

function selectFile(container: HTMLElement): void {
  const input = container.querySelector<HTMLInputElement>('input[type="file"]');
  expect(input).not.toBeNull();
  if (input === null) {
    return;
  }
  const file = new File(['%PDF-1.7 lab report'], 'lab-report.pdf', { type: 'application/pdf' });
  fireEvent.change(input, { target: { files: [file] } });
}

describe('PatientHero document upload → extraction seam', () => {
  it('threads the accepted document_id into the extraction panel', async () => {
    const uploadDocument = vi.fn().mockResolvedValue(ACCEPTED);
    const fetchDocument = vi.fn().mockResolvedValue(DETAIL);

    const { container } = renderHero({ uploadDocument, fetchDocument });

    // No panel before anything is accepted.
    expect(screen.queryByRole('region', { name: 'Uploaded document extraction' })).toBeNull();

    selectFile(container);
    await screen.findByText(/accepted — extracting/i);

    // The 202's document_id survives the upload and drives the reader…
    await screen.findByRole('region', { name: 'Uploaded document extraction' });
    await screen.findByText('9.1 g/dL');
    expect(fetchDocument).toHaveBeenCalledWith('doc-42');

    // …and the extracted fact carries a document-variant citation chip.
    const chip = screen.getByRole('button', { name: 'Source: Doc p.3' });
    expect(chip.getAttribute('data-variant')).toBe('document');
  });

  it('renders no extraction panel when no document reader is wired', async () => {
    const uploadDocument = vi.fn().mockResolvedValue(ACCEPTED);

    const { container } = renderHero({ uploadDocument });
    selectFile(container);
    await screen.findByText(/accepted — extracting/i);

    expect(screen.queryByRole('region', { name: 'Uploaded document extraction' })).toBeNull();
  });
});
