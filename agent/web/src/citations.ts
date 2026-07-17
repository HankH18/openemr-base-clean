/**
 * Citation adapter — maps a backend citation (the Week 2 `Citation` union of
 * fhir/document/guideline sources, or a legacy Week 1 `SourceRef`) onto the
 * one chip model `ProvenanceChip` renders.
 *
 * Parse, don't validate: the input is treated as `unknown` and narrowed field
 * by field, so a claim with a malformed or not-yet-understood citation still
 * renders a chip — the fail-safe `unknown` variant — instead of crashing the
 * card. A citation the UI cannot classify is surfaced as unverified, never
 * silently dressed up as a record citation.
 */

import type { Citation, SourceRef } from './api/types';
import { fmtStamp } from './fmt';
import { humanizeLabel } from './labels';

export type CitationVariant = 'fhir' | 'document' | 'guideline' | 'unknown';

/** One term/value row in the chip's popover. `emphasis` marks the quote row. */
export interface CitationDetail {
  term: string;
  value: string;
  emphasis?: boolean;
}

/** Everything the chip needs to render one citation, whatever its source. */
export interface CitationChipModel {
  variant: CitationVariant;
  /** Short label on the inline chip (styled uppercase). */
  chipLabel: string;
  /** Leading glyph on the chip — decorative, aria-hidden. */
  glyph: string;
  /** Popover rows, in display order. */
  details: CitationDetail[];
  /** Footnote under the rows. */
  note: string;
  /** Normalized [x, y, w, h] — document citations only, else null. */
  bbox: number[] | null;
  /** Cited page — document citations only, else null. */
  pageNumber: number | null;
  /** The cited document/guideline id, when the source carries one. */
  sourceId: string | null;
  /**
   * Verbatim quoted text — labels the overlay's highlighted region.
   * Document citations only, else null.
   */
  quote: string | null;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null;
}

/** The string at `key`, or '' when absent/not a string. */
function str(raw: Record<string, unknown>, key: string): string {
  const value = raw[key];
  return typeof value === 'string' ? value : '';
}

/** A normalized [x, y, w, h] bbox, or null when the shape is unusable. */
function normalizedBbox(value: unknown): number[] | null {
  if (!Array.isArray(value) || value.length !== 4) {
    return null;
  }
  const parts = value.filter((n): n is number => typeof n === 'number' && Number.isFinite(n));
  return parts.length === 4 ? parts : null;
}

function unknownModel(raw: unknown): CitationChipModel {
  const details: CitationDetail[] = [];
  if (isRecord(raw)) {
    const claimed = str(raw, 'value') || str(raw, 'quote_or_value');
    if (claimed !== '') {
      details.push({ term: 'Claimed value', value: claimed, emphasis: true });
    }
  }
  return {
    variant: 'unknown',
    chipLabel: 'Source',
    glyph: '?',
    details,
    note: 'This citation could not be matched to a known source type — treat it as unverified.',
    bbox: null,
    pageNumber: null,
    sourceId: null,
    quote: null,
  };
}

function fhirModel(raw: Record<string, unknown>): CitationChipModel {
  const resourceType = str(raw, 'resource_type');
  const label = resourceType !== '' ? humanizeLabel(resourceType) : 'Record';
  const details: CitationDetail[] = [{ term: 'Resource', value: label }];
  const value = str(raw, 'value');
  if (value !== '') {
    details.push({ term: 'Recorded value', value, emphasis: true });
  }
  const timestamp = raw['timestamp'];
  if (typeof timestamp === 'string' && timestamp !== '') {
    details.push({ term: 'Recorded', value: fmtStamp(timestamp) });
  }
  return {
    variant: 'fhir',
    chipLabel: label,
    glyph: '✓',
    details,
    note: 'Quoted verbatim from the source record.',
    bbox: null,
    pageNumber: null,
    sourceId: null,
    quote: null,
  };
}

function documentModel(raw: Record<string, unknown>): CitationChipModel {
  const page = raw['page_or_section'];
  const pageNumber = typeof page === 'number' && Number.isFinite(page) ? page : null;
  const sourceId = str(raw, 'source_id');
  const details: CitationDetail[] = [];
  if (sourceId !== '') {
    details.push({ term: 'Document', value: sourceId });
  }
  if (pageNumber !== null) {
    details.push({ term: 'Page', value: String(pageNumber) });
  }
  const quote = str(raw, 'quote_or_value');
  if (quote !== '') {
    details.push({ term: 'Quoted text', value: quote, emphasis: true });
  }
  const confidence = raw['confidence'];
  if (typeof confidence === 'number' && Number.isFinite(confidence)) {
    details.push({ term: 'Match', value: `${Math.round(confidence * 100)}%` });
  }
  return {
    variant: 'document',
    chipLabel: pageNumber !== null ? `Doc p.${pageNumber}` : 'Document',
    glyph: '¶',
    details,
    note: 'Quoted from the uploaded document; the highlighted region shows exactly where.',
    bbox: normalizedBbox(raw['bbox']),
    pageNumber,
    sourceId: sourceId !== '' ? sourceId : null,
    quote: quote !== '' ? quote : null,
  };
}

function guidelineModel(raw: Record<string, unknown>): CitationChipModel {
  const sourceId = str(raw, 'source_id');
  const section = str(raw, 'page_or_section');
  const details: CitationDetail[] = [];
  if (sourceId !== '') {
    details.push({ term: 'Guideline', value: sourceId });
  }
  if (section !== '') {
    details.push({ term: 'Section', value: section });
  }
  const quote = str(raw, 'quote_or_value');
  if (quote !== '') {
    details.push({ term: 'Passage', value: quote, emphasis: true });
  }
  return {
    variant: 'guideline',
    chipLabel: 'Guideline',
    glyph: '§',
    details,
    note: 'Guideline evidence — supporting literature, not this patient’s record.',
    bbox: null,
    pageNumber: null,
    sourceId: sourceId !== '' ? sourceId : null,
    quote: null,
  };
}

/**
 * Map one backend citation onto the chip model. Never throws: anything that
 * is not a recognizable fhir/document/guideline citation — including a
 * completely foreign `source_type`, or non-object garbage — degrades to the
 * `unknown` variant. A legacy `SourceRef` (no `source_type`, but shaped like
 * a record citation) defaults to `fhir`, mirroring the backend deserializer.
 */
export function adaptCitation(raw: SourceRef | Citation | unknown): CitationChipModel {
  if (!isRecord(raw)) {
    return unknownModel(raw);
  }
  const tag = raw['source_type'];
  if (tag === 'fhir') {
    return fhirModel(raw);
  }
  if (tag === 'document') {
    return documentModel(raw);
  }
  if (tag === 'guideline') {
    return guidelineModel(raw);
  }
  if (tag === undefined && typeof raw['resource_type'] === 'string') {
    // Week 1 SourceRef — no discriminator; default to the record variant.
    return fhirModel(raw);
  }
  return unknownModel(raw);
}
