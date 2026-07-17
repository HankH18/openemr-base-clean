import { afterEach, describe, expect, it, vi } from 'vitest';
import { documentDetailFrom, getDocument } from './documents';
import { ApiError } from './types';

type FetchFn = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;

function jsonResponse(status: number, payload: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: () => Promise.resolve(payload),
  } as unknown as Response;
}

/**
 * The exact GET /v1/documents/{id} body the service emits (see `_fact_body`
 * and `_citation_body` in copilot/api/routes/documents.py): the document and
 * fact ids arrive as bare numbers, citations stringify theirs, and an
 * unsupported fact carries nulls and no citation.
 */
const WIRE_DETAIL = {
  document_id: 19,
  patient_id: 1003,
  status: 'extracted',
  doc_type: 'lab_pdf',
  page_count: 3,
  openemr_document_id: null,
  correlation_id: 'corr-9',
  extraction: {
    extraction_id: 4,
    model: 'vision-1',
    schema_version: 'lab.v1',
    confidence_overall: 0.91,
    facts: [
      {
        id: 7,
        field_path: 'hemoglobin',
        value: '9.1',
        unit: 'g/dL',
        reference_range: '13.5-17.5',
        abnormal_flag: 'L',
        page_no: 3,
        bbox: [0.25, 0.5, 0.5, 0.25],
        match_confidence: 0.92,
        supported: true,
      },
      {
        id: 8,
        field_path: 'specimen_source',
        value: 'Venous draw',
        unit: null,
        reference_range: null,
        abnormal_flag: null,
        page_no: null,
        bbox: null,
        match_confidence: null,
        supported: false,
      },
    ],
  },
  citations: [
    {
      source_type: 'document',
      source_id: '19',
      page_or_section: 3,
      field_or_chunk_id: '7',
      quote_or_value: '9.1',
      bbox: [0.25, 0.5, 0.5, 0.25],
      confidence: 0.92,
    },
  ],
};

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('getDocument', () => {
  it('GETs /v1/documents/{id} with credentials and the asserted clinician', async () => {
    const fetchMock = vi.fn<FetchFn>().mockResolvedValue(jsonResponse(200, WIRE_DETAIL));
    vi.stubGlobal('fetch', fetchMock);

    const detail = await getDocument('19', 501);

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const call = fetchMock.mock.calls[0];
    expect(call).toBeDefined();
    if (call === undefined) {
      return;
    }
    const [url, init] = call;
    expect(String(url)).toBe('/v1/documents/19?clinician_id=501');
    expect(init?.credentials).toBe('include');
    expect(detail.status).toBe('extracted');
  });

  it('omits clinician_id when none is asserted (smart mode: the cookie is identity)', async () => {
    const fetchMock = vi.fn<FetchFn>().mockResolvedValue(jsonResponse(200, WIRE_DETAIL));
    vi.stubGlobal('fetch', fetchMock);

    await getDocument('19');

    expect(String(fetchMock.mock.calls[0]?.[0])).toBe('/v1/documents/19');
  });

  it('normalizes the wire payload: string ids, joined citations, honest nulls', async () => {
    vi.stubGlobal('fetch', vi.fn<FetchFn>().mockResolvedValue(jsonResponse(200, WIRE_DETAIL)));

    const detail = await getDocument('19', 501);

    expect(detail.document_id).toBe('19');
    expect(detail.patient_id).toBe(1003);
    expect(detail.doc_type).toBe('lab_pdf');
    expect(detail.page_count).toBe(3);
    expect(detail.facts).toHaveLength(2);

    const [hgb, specimen] = detail.facts;
    // The supported fact: number id stringified, citation joined by
    // field_or_chunk_id, geometry intact.
    expect(hgb?.id).toBe('7');
    expect(hgb?.supported).toBe(true);
    expect(hgb?.page_no).toBe(3);
    expect(hgb?.bbox).toEqual([0.25, 0.5, 0.5, 0.25]);
    expect(hgb?.citation).not.toBeNull();
    expect(hgb?.citation?.source_type).toBe('document');
    expect(hgb?.citation?.source_id).toBe('19');
    expect(hgb?.citation?.page_or_section).toBe(3);
    expect(hgb?.citation?.bbox).toEqual([0.25, 0.5, 0.5, 0.25]);
    expect(hgb?.citation?.quote_or_value).toBe('9.1');

    // The unsupported fact: nulls stay null, no citation is fabricated,
    // and null strings normalize to ''.
    expect(specimen?.supported).toBe(false);
    expect(specimen?.citation).toBeNull();
    expect(specimen?.bbox).toBeNull();
    expect(specimen?.page_no).toBeNull();
    expect(specimen?.unit).toBe('');
    expect(specimen?.reference_range).toBe('');
  });

  it('throws a typed ApiError on a non-2xx reply so the poller can retry', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn<FetchFn>().mockResolvedValue(jsonResponse(403, { detail: 'nope' })),
    );

    await expect(getDocument('19', 501)).rejects.toMatchObject({
      name: 'ApiError',
      status: 403,
    });
  });

  it('throws a typed ApiError when the service is unreachable', async () => {
    vi.stubGlobal('fetch', vi.fn<FetchFn>().mockRejectedValue(new TypeError('network down')));

    await expect(getDocument('19', 501)).rejects.toBeInstanceOf(ApiError);
  });
});

describe('documentDetailFrom', () => {
  it('normalizes garbage to a safe empty detail instead of throwing', () => {
    for (const junk of [null, undefined, 42, 'nope', [], { extraction: 'x', citations: 3 }]) {
      const detail = documentDetailFrom(junk, 'doc-5');
      expect(detail.document_id).toBe('doc-5');
      expect(detail.facts).toEqual([]);
      expect(detail.page_count).toBeNull();
    }
  });

  it('drops unreadable fact entries and keeps the readable ones', () => {
    const detail = documentDetailFrom(
      {
        document_id: '3',
        patient_id: 1004,
        status: 'extracted',
        doc_type: 'intake_form',
        page_count: 1,
        extraction: {
          facts: [
            null,
            'garbage',
            { id: 1 }, // neither a field label nor a value — nothing to show
            { id: 2, field_path: 'chief_complaint', value: 'Shortness of breath' },
          ],
        },
        citations: [],
      },
      '3',
    );

    expect(detail.facts).toHaveLength(1);
    expect(detail.facts[0]?.field_path).toBe('chief_complaint');
    // Absent optional provenance reads as honest nulls/empties, not failures.
    expect(detail.facts[0]?.bbox).toBeNull();
    expect(detail.facts[0]?.citation).toBeNull();
    expect(detail.facts[0]?.supported).toBe(true);
  });

  it('keeps a citation quote but drops its geometry when the cited page is unusable', () => {
    const detail = documentDetailFrom(
      {
        document_id: 4,
        patient_id: 1003,
        status: 'extracted',
        doc_type: 'lab_pdf',
        page_count: 2,
        extraction: {
          facts: [{ id: 11, field_path: 'lactate', value: '4.2', supported: true }],
        },
        citations: [
          {
            source_type: 'document',
            source_id: '4',
            page_or_section: 'not-a-page',
            field_or_chunk_id: '11',
            quote_or_value: '4.2',
            bbox: [0.1, 0.2, 0.3, 0.4],
            confidence: 0.8,
          },
        ],
      },
      '4',
    );

    const citation = detail.facts[0]?.citation;
    expect(citation).not.toBeNull();
    expect(citation?.quote_or_value).toBe('4.2');
    // Without a real page the box cannot be drawn honestly: empty geometry,
    // which the chip adapter degrades to the text-only popover.
    expect(citation?.bbox).toEqual([]);
  });

  it('drops a citation with no join key rather than attaching it to the wrong fact', () => {
    const detail = documentDetailFrom(
      {
        document_id: 5,
        status: 'extracted',
        extraction: {
          facts: [{ id: 21, field_path: 'sodium', value: '129', supported: true }],
        },
        citations: [
          {
            source_type: 'document',
            source_id: '5',
            page_or_section: 1,
            quote_or_value: '129',
            bbox: [0.1, 0.1, 0.2, 0.05],
            confidence: 0.9,
          },
        ],
      },
      '5',
    );

    expect(detail.facts[0]?.citation).toBeNull();
  });
});
