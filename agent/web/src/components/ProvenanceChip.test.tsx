import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { ProvenanceChip } from './ProvenanceChip';
import type { Citation, SourceRef } from '../api/types';

const FHIR: Citation = {
  source_type: 'fhir',
  resource_type: 'MedicationRequest',
  resource_id: 'med-42',
  field: 'medicationCodeableConcept',
  value: 'Ceftriaxone 1 g IV q24h',
  timestamp: '2026-07-09T05:30:00Z',
};

const DOCUMENT: Citation = {
  source_type: 'document',
  source_id: 'doc-19',
  page_or_section: 3,
  field_or_chunk_id: 'fact-2',
  quote_or_value: 'Hemoglobin 9.1 g/dL',
  bbox: [0.25, 0.5, 0.5, 0.25],
  confidence: 0.92,
};

const GUIDELINE: Citation = {
  source_type: 'guideline',
  source_id: 'surviving-sepsis-2021',
  page_or_section: 'Fluid resuscitation',
  field_or_chunk_id: 'chunk-88',
  quote_or_value: 'We recommend at least 30 mL/kg of IV crystalloid within 3 h.',
};

function chipButton(): HTMLElement {
  const button = screen.getByRole('button');
  expect(button).toBeTruthy();
  return button;
}

/** Press the chip and wait for its React Aria popover dialog to appear. */
async function openPopover(): Promise<HTMLElement> {
  fireEvent.click(chipButton());
  return screen.findByRole('dialog');
}

/** jsdom never decodes images, so intrinsic dimensions are stubbed per test. */
function setIntrinsicSize(image: HTMLImageElement, width: number, height: number): void {
  Object.defineProperty(image, 'naturalWidth', { value: width, configurable: true });
  Object.defineProperty(image, 'naturalHeight', { value: height, configurable: true });
}

describe('ProvenanceChip', () => {
  it('renders the fhir variant with the humanized resource label', () => {
    render(<ProvenanceChip source={FHIR} />);
    const button = chipButton();
    expect(button.getAttribute('data-variant')).toBe('fhir');
    expect(button.className).toContain('prov-chip--fhir');
    expect(button.textContent).toContain('Medication');
    expect(button.textContent).toContain('✓');
  });

  it('renders a legacy source_ref as the fhir variant', () => {
    const legacy: SourceRef = {
      resource_type: 'Observation',
      resource_id: 'obs-7',
      field: 'valueQuantity',
      value: '4.2',
    };
    render(<ProvenanceChip source={legacy} />);
    expect(chipButton().getAttribute('data-variant')).toBe('fhir');
  });

  it('renders the document variant distinctly with its page number', () => {
    render(<ProvenanceChip source={DOCUMENT} />);
    const button = chipButton();
    expect(button.getAttribute('data-variant')).toBe('document');
    expect(button.className).toContain('prov-chip--document');
    expect(button.textContent).toContain('Doc p.3');
  });

  it('renders the guideline variant distinctly', () => {
    render(<ProvenanceChip source={GUIDELINE} />);
    const button = chipButton();
    expect(button.getAttribute('data-variant')).toBe('guideline');
    expect(button.className).toContain('prov-chip--guideline');
    expect(button.textContent).toContain('Guideline');
    expect(button.textContent).toContain('§');
  });

  it('gives each of the three variants a distinct look and label', () => {
    const seen = [FHIR, DOCUMENT, GUIDELINE].map((source) => {
      const { container, unmount } = render(<ProvenanceChip source={source} />);
      const button = container.querySelector('button');
      expect(button).not.toBeNull();
      const fingerprint = {
        variant: button?.getAttribute('data-variant') ?? '',
        label: button?.textContent ?? '',
      };
      unmount();
      return fingerprint;
    });
    const variants = new Set(seen.map((s) => s.variant));
    const labels = new Set(seen.map((s) => s.label));
    expect(variants.size).toBe(3);
    expect(labels.size).toBe(3);
  });

  it('boxes the cited region on the page image when a document citation opens', async () => {
    render(<ProvenanceChip source={DOCUMENT} />);
    const dialog = await openPopover();

    // The page image URL goes through the documents API module (shared
    // base-URL normalization — '' → same-origin in tests).
    const probe = dialog.querySelector<HTMLImageElement>('img.evidence-probe');
    expect(probe).not.toBeNull();
    if (probe === null) {
      return;
    }
    expect(probe.getAttribute('src')).toBe('/v1/documents/doc-19/pages/3');

    setIntrinsicSize(probe, 1000, 800);
    fireEvent.load(probe);

    // EvidenceOverlay renders in the popover with the probed intrinsic
    // dimensions, and the citation's bbox lands on the right pixels.
    const svg = dialog.querySelector('svg.evidence-svg');
    expect(svg?.getAttribute('viewBox')).toBe('0 0 1000 800');
    const rect = dialog.querySelector('rect.evidence-box');
    expect(rect).not.toBeNull();
    expect(Number.parseFloat(rect?.getAttribute('x') ?? '')).toBeCloseTo(250, 5);
    expect(Number.parseFloat(rect?.getAttribute('y') ?? '')).toBeCloseTo(400, 5);
    expect(Number.parseFloat(rect?.getAttribute('width') ?? '')).toBeCloseTo(500, 5);
    expect(Number.parseFloat(rect?.getAttribute('height') ?? '')).toBeCloseTo(200, 5);
    expect(rect?.querySelector('title')?.textContent).toBe('Hemoglobin 9.1 g/dL');

    const page = dialog.querySelector('img.evidence-page');
    expect(page?.getAttribute('alt')).toContain('page 3');

    // The text details stay alongside the visual.
    expect(dialog.textContent).toContain('Hemoglobin 9.1 g/dL');
    expect(dialog.textContent).toContain('92%');
  });

  it('falls back to the text-only popover when the page image fails to load', async () => {
    render(<ProvenanceChip source={DOCUMENT} />);
    const dialog = await openPopover();

    const probe = dialog.querySelector<HTMLImageElement>('img.evidence-probe');
    expect(probe).not.toBeNull();
    if (probe === null) {
      return;
    }
    fireEvent.error(probe);

    // No broken image, no overlay — the text details carry the citation.
    expect(dialog.querySelector('img')).toBeNull();
    expect(dialog.querySelector('svg')).toBeNull();
    expect(dialog.textContent).toContain('Hemoglobin 9.1 g/dL');
    expect(dialog.textContent).toContain('Page');
    expect(dialog.textContent).toContain('92%');
  });

  it('never renders a page image for non-document citations', async () => {
    render(<ProvenanceChip source={GUIDELINE} />);
    const dialog = await openPopover();

    expect(dialog.querySelector('img')).toBeNull();
    expect(dialog.querySelector('svg')).toBeNull();
    expect(dialog.textContent).toContain('Fluid resuscitation');
  });

  it('renders a safe fallback chip for an unknown citation type instead of crashing', () => {
    const alien = {
      source_type: 'telepathy',
      quote_or_value: 'trust me',
    } as unknown as Citation;
    render(<ProvenanceChip source={alien} />);
    const button = chipButton();
    expect(button.getAttribute('data-variant')).toBe('unknown');
    expect(button.className).toContain('prov-chip--unknown');
    expect(button.textContent).toContain('Source');
    expect(button.textContent).toContain('?');
  });
});
