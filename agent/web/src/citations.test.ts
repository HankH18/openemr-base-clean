import { describe, expect, it } from 'vitest';
import { adaptCitation } from './citations';
import type { Citation, SourceRef } from './api/types';

describe('citation adapter', () => {
  it('maps a fhir citation to the record chip model', () => {
    const citation: Citation = {
      source_type: 'fhir',
      resource_type: 'MedicationRequest',
      resource_id: 'med-42',
      field: 'medicationCodeableConcept',
      value: 'Ceftriaxone 1 g IV q24h',
      timestamp: '2026-07-09T05:30:00Z',
    };
    const model = adaptCitation(citation);
    expect(model.variant).toBe('fhir');
    expect(model.chipLabel).toBe('Medication');
    expect(model.details.map((d) => d.term)).toEqual(['Resource', 'Recorded value', 'Recorded']);
    expect(model.details.find((d) => d.emphasis)?.value).toBe('Ceftriaxone 1 g IV q24h');
    expect(model.bbox).toBeNull();
    expect(model.quote).toBeNull();
  });

  it('defaults a legacy source_ref without source_type to the fhir variant', () => {
    const legacy: SourceRef = {
      resource_type: 'Observation',
      resource_id: 'obs-7',
      field: 'valueQuantity',
      value: '4.2',
    };
    const model = adaptCitation(legacy);
    expect(model.variant).toBe('fhir');
    expect(model.chipLabel).toBe('Observation');
  });

  it('maps a document citation with page number and normalized bbox', () => {
    const citation: Citation = {
      source_type: 'document',
      source_id: 'doc-19',
      page_or_section: 3,
      field_or_chunk_id: 'fact-2',
      quote_or_value: 'Hemoglobin 9.1 g/dL',
      bbox: [0.25, 0.5, 0.5, 0.25],
      confidence: 0.92,
    };
    const model = adaptCitation(citation);
    expect(model.variant).toBe('document');
    expect(model.chipLabel).toBe('Doc p.3');
    expect(model.pageNumber).toBe(3);
    expect(model.sourceId).toBe('doc-19');
    expect(model.bbox).toEqual([0.25, 0.5, 0.5, 0.25]);
    expect(model.quote).toBe('Hemoglobin 9.1 g/dL');
    expect(model.details.find((d) => d.term === 'Match')?.value).toBe('92%');
    expect(model.details.find((d) => d.emphasis)?.value).toBe('Hemoglobin 9.1 g/dL');
  });

  it('drops a malformed document bbox rather than passing bad geometry through', () => {
    const model = adaptCitation({
      source_type: 'document',
      source_id: 'doc-20',
      page_or_section: 1,
      field_or_chunk_id: 'fact-9',
      quote_or_value: 'WBC 14.2',
      bbox: [0.1, 0.2, Number.NaN],
      confidence: 0.4,
    });
    expect(model.variant).toBe('document');
    expect(model.bbox).toBeNull();
  });

  it('maps a guideline citation to a labeled guideline chip', () => {
    const citation: Citation = {
      source_type: 'guideline',
      source_id: 'surviving-sepsis-2021',
      page_or_section: 'Fluid resuscitation',
      field_or_chunk_id: 'chunk-88',
      quote_or_value: 'We recommend at least 30 mL/kg of IV crystalloid within 3 h.',
    };
    const model = adaptCitation(citation);
    expect(model.variant).toBe('guideline');
    expect(model.chipLabel).toBe('Guideline');
    expect(model.details.map((d) => d.term)).toEqual(['Guideline', 'Section', 'Passage']);
    expect(model.note).toMatch(/not this patient/i);
  });

  it('falls back safely on an unknown source_type', () => {
    const model = adaptCitation({
      source_type: 'telepathy',
      quote_or_value: 'trust me',
    });
    expect(model.variant).toBe('unknown');
    expect(model.chipLabel).toBe('Source');
    expect(model.note).toMatch(/unverified/i);
    expect(model.details.find((d) => d.emphasis)?.value).toBe('trust me');
  });

  it('falls back safely on garbage input without throwing', () => {
    for (const garbage of [null, undefined, 42, 'citation', [], {}]) {
      const model = adaptCitation(garbage);
      expect(model.variant).toBe('unknown');
      expect(model.chipLabel).toBe('Source');
    }
  });
});
