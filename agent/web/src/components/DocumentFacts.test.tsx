import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { ApiError, type DocumentCitation, type DocumentDetail, type ExtractedFact } from '../api/types';
import { DocumentFacts } from './DocumentFacts';

function citation(overrides: Partial<DocumentCitation> = {}): DocumentCitation {
  return {
    source_type: 'document',
    source_id: '19',
    page_or_section: 3,
    field_or_chunk_id: '7',
    quote_or_value: 'Hemoglobin 9.1 g/dL',
    bbox: [0.25, 0.5, 0.5, 0.25],
    confidence: 0.92,
    ...overrides,
  };
}

function fact(overrides: Partial<ExtractedFact> = {}): ExtractedFact {
  return {
    id: '7',
    field_path: 'hemoglobin',
    value: '9.1',
    unit: 'g/dL',
    reference_range: '13.5-17.5',
    abnormal_flag: 'L',
    page_no: 3,
    bbox: [0.25, 0.5, 0.5, 0.25],
    match_confidence: 0.92,
    supported: true,
    citation: citation(),
    ...overrides,
  };
}

function detail(status: string, facts: ExtractedFact[]): DocumentDetail {
  return {
    document_id: '19',
    patient_id: 1003,
    status,
    doc_type: 'lab_pdf',
    page_count: 3,
    facts,
  };
}

/** jsdom never decodes images, so intrinsic dimensions are stubbed per test. */
function setIntrinsicSize(image: HTMLImageElement, width: number, height: number): void {
  Object.defineProperty(image, 'naturalWidth', { value: width, configurable: true });
  Object.defineProperty(image, 'naturalHeight', { value: height, configurable: true });
}

describe('DocumentFacts polling', () => {
  it('shows the pending state, then the extracted facts once the status is terminal', async () => {
    const fetchDocument = vi
      .fn<(documentId: string) => Promise<DocumentDetail>>()
      .mockResolvedValueOnce(detail('processing', []))
      .mockResolvedValue(detail('extracted', [fact()]));

    render(
      <DocumentFacts documentId="19" fetchDocument={fetchDocument} pollIntervalMs={1} />,
    );

    const pending = await screen.findByRole('status');
    expect(pending.textContent).toMatch(/extracting/i);

    await screen.findByText('9.1 g/dL');
    expect(fetchDocument).toHaveBeenCalledTimes(2);
    expect(fetchDocument).toHaveBeenCalledWith('19');
    // Terminal — no further polling states are shown.
    expect(screen.queryByRole('status')).toBeNull();
  });

  it('stops at the poll cap and says extraction is still running', async () => {
    const fetchDocument = vi
      .fn<(documentId: string) => Promise<DocumentDetail>>()
      .mockResolvedValue(detail('processing', []));

    render(
      <DocumentFacts
        documentId="19"
        fetchDocument={fetchDocument}
        pollIntervalMs={1}
        maxPolls={3}
      />,
    );

    await screen.findByText(/taking longer than expected/i);
    expect(fetchDocument).toHaveBeenCalledTimes(3);
  });

  it('reports a failed extraction honestly', async () => {
    const fetchDocument = vi
      .fn<(documentId: string) => Promise<DocumentDetail>>()
      .mockResolvedValue(detail('failed', []));

    render(<DocumentFacts documentId="19" fetchDocument={fetchDocument} pollIntervalMs={1} />);

    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toMatch(/extraction failed/i);
  });

  it('reports an unreachable service instead of spinning forever', async () => {
    const fetchDocument = vi
      .fn<(documentId: string) => Promise<DocumentDetail>>()
      .mockRejectedValue(new Error('network down'));

    render(
      <DocumentFacts
        documentId="19"
        fetchDocument={fetchDocument}
        pollIntervalMs={1}
        maxPolls={2}
      />,
    );

    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toMatch(/unreachable/i);
    expect(fetchDocument).toHaveBeenCalledTimes(2);
  });

  it('stops immediately on a definitive refusal instead of hammering the endpoint', async () => {
    const fetchDocument = vi
      .fn<(documentId: string) => Promise<DocumentDetail>>()
      .mockRejectedValue(new ApiError('Record service replied 403', 403));

    render(<DocumentFacts documentId="19" fetchDocument={fetchDocument} pollIntervalMs={1} />);

    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toMatch(/replied 403/);
    expect(fetchDocument).toHaveBeenCalledTimes(1);
  });

  it('says so plainly when extraction finishes with zero facts', async () => {
    const fetchDocument = vi
      .fn<(documentId: string) => Promise<DocumentDetail>>()
      .mockResolvedValue(detail('extracted', []));

    render(<DocumentFacts documentId="19" fetchDocument={fetchDocument} pollIntervalMs={1} />);

    await screen.findByText(/no facts could be read/i);
  });
});

describe('DocumentFacts rendering', () => {
  it('renders a document-variant chip that opens the page with the cited region boxed', async () => {
    const fetchDocument = vi
      .fn<(documentId: string) => Promise<DocumentDetail>>()
      .mockResolvedValue(detail('extracted', [fact()]));

    render(<DocumentFacts documentId="19" fetchDocument={fetchDocument} pollIntervalMs={1} />);

    await screen.findByText('9.1 g/dL');
    const chip = screen.getByRole('button', { name: 'Source: Doc p.3' });
    expect(chip.getAttribute('data-variant')).toBe('document');

    // Click-to-source: the popover loads the cited page image…
    fireEvent.click(chip);
    const dialog = await screen.findByRole('dialog');
    const probe = dialog.querySelector<HTMLImageElement>('img.evidence-probe');
    expect(probe).not.toBeNull();
    if (probe === null) {
      return;
    }
    expect(probe.getAttribute('src')).toBe('/v1/documents/19/pages/3');

    // …and once its intrinsic size is known, the bbox is drawn over it.
    setIntrinsicSize(probe, 1000, 800);
    fireEvent.load(probe);
    const svg = dialog.querySelector('svg.evidence-svg');
    expect(svg?.getAttribute('viewBox')).toBe('0 0 1000 800');
    const rect = dialog.querySelector('rect.evidence-box');
    expect(rect).not.toBeNull();
    expect(Number.parseFloat(rect?.getAttribute('x') ?? '')).toBeCloseTo(250, 5);
    expect(Number.parseFloat(rect?.getAttribute('y') ?? '')).toBeCloseTo(400, 5);
    expect(Number.parseFloat(rect?.getAttribute('width') ?? '')).toBeCloseTo(500, 5);
    expect(Number.parseFloat(rect?.getAttribute('height') ?? '')).toBeCloseTo(200, 5);
  });

  it('marks an unsupported fact visibly and gives it no source chip', async () => {
    const rows = [
      fact(),
      fact({
        id: '8',
        field_path: 'specimen_source',
        value: 'Venous draw',
        unit: '',
        reference_range: '',
        abnormal_flag: '',
        page_no: null,
        bbox: null,
        match_confidence: null,
        supported: false,
        citation: null,
      }),
    ];
    const fetchDocument = vi
      .fn<(documentId: string) => Promise<DocumentDetail>>()
      .mockResolvedValue(detail('extracted', rows));

    const { container } = render(
      <DocumentFacts documentId="19" fetchDocument={fetchDocument} pollIntervalMs={1} />,
    );

    await screen.findByText('Venous draw');
    expect(screen.getByText(/not found on page/i)).toBeTruthy();

    const unsupportedRow = container.querySelector('.claim--unsupported');
    expect(unsupportedRow).not.toBeNull();
    expect(unsupportedRow?.textContent).toContain('Venous draw');
    expect(unsupportedRow?.querySelector('button')).toBeNull();

    // The supported row keeps its chip; only one chip renders in total.
    expect(screen.getAllByRole('button')).toHaveLength(1);
  });

  it('degrades a cited fact without geometry to the text-only popover', async () => {
    const noBox = fact({
      bbox: null,
      citation: citation({ bbox: [] }),
    });
    const fetchDocument = vi
      .fn<(documentId: string) => Promise<DocumentDetail>>()
      .mockResolvedValue(detail('extracted', [noBox]));

    render(<DocumentFacts documentId="19" fetchDocument={fetchDocument} pollIntervalMs={1} />);

    await screen.findByText('9.1 g/dL');
    const chip = screen.getByRole('button', { name: 'Source: Doc p.3' });
    expect(chip.getAttribute('data-variant')).toBe('document');

    fireEvent.click(chip);
    const dialog = await screen.findByRole('dialog');
    // No image, no overlay — never a broken box. The text rows carry it.
    expect(dialog.querySelector('img')).toBeNull();
    expect(dialog.querySelector('svg')).toBeNull();
    expect(dialog.textContent).toContain('Hemoglobin 9.1 g/dL');
    expect(dialog.textContent).toContain('Page');
  });
});
