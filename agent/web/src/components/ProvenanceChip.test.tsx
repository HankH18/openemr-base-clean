import { render, screen } from '@testing-library/react';
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
