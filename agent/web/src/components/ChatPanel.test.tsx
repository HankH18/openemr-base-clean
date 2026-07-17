import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import type { GuidelineEvidenceItem } from '../api/types';
import type { ChatMessage } from '../state/useChat';
import { ChatPanel } from './ChatPanel';

function answer(overrides: Partial<ChatMessage>): ChatMessage {
  return {
    id: 'msg-1',
    kind: 'answer',
    text: 'Latest troponin I is 1.8 ng/mL.',
    claims: [],
    guidelineEvidence: [],
    verification: { action: 'served', passed: true },
    correlationId: 'corr-1',
    pending: false,
    ...overrides,
  };
}

function evidence(overrides: Partial<GuidelineEvidenceItem> = {}): GuidelineEvidenceItem {
  return {
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
      quote_or_value: 'Administer 30 mL/kg IV crystalloid within the first 3 hours.',
    },
    ...overrides,
  };
}

function renderPanel(messages: ChatMessage[]): void {
  render(
    <ChatPanel
      given="Amara"
      messages={messages}
      busy={false}
      suggestions={[]}
      onSend={vi.fn()}
    />,
  );
}

describe('ChatPanel guideline evidence block', () => {
  it('renders retrieved guideline snippets as a distinct labeled block', () => {
    renderPanel([
      answer({
        guidelineEvidence: [
          evidence(),
          evidence({
            chunk_id: 'chunk-9',
            section: 'Vasopressors',
            content: 'Norepinephrine is the first-line vasopressor.',
            citation: null,
          }),
        ],
      }),
    ]);

    const block = screen.getByRole('region', { name: 'Guideline evidence' });
    expect(block.textContent).toContain('Guideline evidence');
    // Each item: section + snippet + source.
    expect(screen.getByText('Sepsis — initial resuscitation')).toBeDefined();
    expect(
      screen.getByText('Administer 30 mL/kg IV crystalloid within the first 3 hours.'),
    ).toBeDefined();
    expect(screen.getByText('Vasopressors')).toBeDefined();
    expect(screen.getByText('Norepinephrine is the first-line vasopressor.')).toBeDefined();
    // Source id comes from the citation when present, else the document row.
    expect(screen.getAllByText('Guideline gl-1')).toHaveLength(2);
    // The framing makes clear this is literature, not the patient's record.
    expect(block.textContent).toContain('not this patient’s record');
  });

  it('renders no guideline block when the evidence array is empty', () => {
    renderPanel([answer({ guidelineEvidence: [] })]);

    expect(screen.queryByRole('region', { name: 'Guideline evidence' })).toBeNull();
    expect(screen.queryByText('Guideline evidence')).toBeNull();
    // The answer itself still renders.
    expect(screen.getByText('Latest troponin I is 1.8 ng/mL.')).toBeDefined();
  });
});
