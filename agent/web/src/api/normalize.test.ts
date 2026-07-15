import { describe, expect, it } from 'vitest';
import { normalizeChat } from './normalize';

function chatWire(extra: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    answer: 'Per the record…',
    claims: [],
    verification: { action: 'served', passed: true },
    conversation_id: 7,
    correlation_id: 'corr-9',
    ...extra,
  };
}

describe('normalizeChat guideline_evidence', () => {
  it('carries the separate guideline-evidence block through, typed', () => {
    const response = normalizeChat(
      chatWire({
        guideline_evidence: [
          {
            source_type: 'guideline',
            chunk_id: 'chunk-3',
            document_id: 'gl-1',
            section: 'Sepsis — initial resuscitation',
            content: 'Administer 30 mL/kg IV crystalloid within the first 3 hours.',
            score: 0.82,
            citation: {
              source_type: 'guideline',
              source_id: 'gl-1',
              page_or_section: 'Sepsis — initial resuscitation',
              field_or_chunk_id: 'chunk-3',
              quote_or_value: 'Administer 30 mL/kg IV crystalloid…',
            },
          },
        ],
      }),
    );

    expect(response.guideline_evidence).toEqual([
      {
        source_type: 'guideline',
        chunk_id: 'chunk-3',
        document_id: 'gl-1',
        section: 'Sepsis — initial resuscitation',
        content: 'Administer 30 mL/kg IV crystalloid within the first 3 hours.',
        score: 0.82,
        citation: {
          source_type: 'guideline',
          source_id: 'gl-1',
          page_or_section: 'Sepsis — initial resuscitation',
          field_or_chunk_id: 'chunk-3',
          quote_or_value: 'Administer 30 mL/kg IV crystalloid…',
        },
      },
    ]);
  });

  it('normalizes an absent block to an empty array (nothing renders)', () => {
    expect(normalizeChat(chatWire()).guideline_evidence).toEqual([]);
  });

  it('drops unusable items and degrades a malformed citation to null', () => {
    const response = normalizeChat(
      chatWire({
        guideline_evidence: [
          'garbage',
          { section: 'No passage text' },
          {
            chunk_id: 'chunk-5',
            document_id: 'gl-2',
            section: 'Vasopressors',
            content: 'Norepinephrine is the first-line vasopressor.',
            score: 'not-a-number',
            citation: { source_id: 42 },
          },
        ],
      }),
    );

    expect(response.guideline_evidence).toEqual([
      {
        source_type: 'guideline',
        chunk_id: 'chunk-5',
        document_id: 'gl-2',
        section: 'Vasopressors',
        content: 'Norepinephrine is the first-line vasopressor.',
        score: 0,
        citation: null,
      },
    ]);
  });
});
