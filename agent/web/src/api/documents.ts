/**
 * Document ingestion — `POST /v1/documents` (multipart). Kept beside the JSON
 * fetch layer (http.ts) rather than inside it because multipart must NOT set
 * a Content-Type header: the browser writes the boundary itself.
 */

import { resolveApiBase } from './base';
import { getCsrfToken } from './session';
import {
  ApiError,
  type DocumentAccepted,
  type DocumentCitation,
  type DocumentDetail,
  type ExtractedFact,
} from './types';

/**
 * The closed set of ingestible document kinds — mirrors the backend
 * `DocumentType` enum (copilot/documents/vision.py) exactly. Modeled as a
 * const tuple + union so an invalid `doc_type` cannot be sent again (the
 * service replies 400 to anything outside this set).
 */
export const DOC_TYPES = ['lab_pdf', 'intake_form', 'medication_list'] as const;

export type DocType = (typeof DOC_TYPES)[number];

/** Physician-facing labels for the upload selector, keyed by wire value. */
export const DOC_TYPE_LABELS: Record<DocType, string> = {
  lab_pdf: 'Lab report (PDF)',
  intake_form: 'Intake form',
  medication_list: 'Medication list',
};

/** Narrow an arbitrary string to a valid `DocType` — parse, don't cast. */
export function isDocType(value: string): value is DocType {
  return (DOC_TYPES as readonly string[]).includes(value);
}

/** Default doc_type for uploads from the rounds UI. */
export const DEFAULT_DOC_TYPE: DocType = 'lab_pdf';

/**
 * URL of one rendered page image — `GET /v1/documents/{id}/pages/{page_no}`
 * (image/png), the backdrop the evidence overlay draws bounding boxes over.
 * Same base-URL normalization as every other call; the browser's <img> fetch
 * carries the session cookie itself (an <img> request includes credentials,
 * matching the JSON layer's `credentials: 'include'`).
 */
export function documentPageUrl(
  documentId: string,
  pageNo: number,
  base: string = resolveApiBase(),
): string {
  return `${base}/v1/documents/${encodeURIComponent(documentId)}/pages/${encodeURIComponent(String(pageNo))}`;
}

// ------------------------------------------------- GET /v1/documents/{id}
//
// Tolerant wire-shape normalization, mirroring normalize.ts conventions for
// best-effort surfaces: a malformed payload becomes a safe empty detail, a
// malformed fact/citation entry is dropped or read field-by-field — the UI
// never throws on a shape the service half-delivered.

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v);
}

/** String read loosely — non-strings normalize to ''. */
function looseString(v: unknown): string {
  return typeof v === 'string' ? v : '';
}

/**
 * An id that may arrive as a string (POST echo, citations) or a number
 * (the GET body serializes row ids bare) — normalized to a string, '' when
 * unreadable. Stringifying both sides is what makes the fact↔citation join
 * on `field_or_chunk_id` reliable.
 */
function idString(v: unknown): string {
  if (typeof v === 'string') {
    return v;
  }
  if (typeof v === 'number' && Number.isFinite(v)) {
    return String(v);
  }
  return '';
}

function finiteNumber(v: unknown): number | null {
  return typeof v === 'number' && Number.isFinite(v) ? v : null;
}

/** `patient_id` may serialize as `{value: n}` or `n` — accept both, else 0. */
function looseId(v: unknown): number {
  if (typeof v === 'number' && Number.isFinite(v)) {
    return v;
  }
  if (isRecord(v) && typeof v['value'] === 'number' && Number.isFinite(v['value'])) {
    return v['value'];
  }
  return 0;
}

/** A normalized [x, y, w, h] bbox, or null when the shape is unusable. */
function bboxFrom(v: unknown): number[] | null {
  if (!Array.isArray(v) || v.length !== 4) {
    return null;
  }
  const parts = v.filter((n): n is number => typeof n === 'number' && Number.isFinite(n));
  return parts.length === 4 ? parts : null;
}

/**
 * One wire citation → a typed `DocumentCitation`, or null when it cannot be
 * used (no join key or no document id). Without a real page number the box
 * cannot be drawn honestly, so the geometry is dropped ([] — the chip then
 * degrades to its text rows) while the quote is kept.
 */
function citationFrom(raw: unknown): DocumentCitation | null {
  if (!isRecord(raw)) {
    return null;
  }
  const factId = idString(raw['field_or_chunk_id']);
  const sourceId = idString(raw['source_id']);
  if (factId === '' || sourceId === '') {
    return null;
  }
  const page = finiteNumber(raw['page_or_section']);
  return {
    source_type: 'document',
    source_id: sourceId,
    page_or_section: page ?? 1,
    field_or_chunk_id: factId,
    quote_or_value: looseString(raw['quote_or_value']),
    bbox: page === null ? [] : (bboxFrom(raw['bbox']) ?? []),
    confidence: finiteNumber(raw['confidence']) ?? 0,
  };
}

/**
 * One wire fact → a typed `ExtractedFact` joined to its citation, or null
 * when there is nothing to show (neither a field label nor a value).
 * `supported` treats only an explicit `false` as unsupported — the marker
 * flags what the no-invention gate rejected, never what it left unsaid.
 */
function factFrom(
  raw: unknown,
  citationsByFact: ReadonlyMap<string, DocumentCitation>,
): ExtractedFact | null {
  if (!isRecord(raw)) {
    return null;
  }
  const id = idString(raw['id']);
  const fieldPath = looseString(raw['field_path']);
  const rawValue = raw['value'];
  const value = typeof rawValue === 'number' ? String(rawValue) : looseString(rawValue);
  if (fieldPath === '' && value === '') {
    return null;
  }
  return {
    id,
    field_path: fieldPath,
    value,
    unit: looseString(raw['unit']),
    reference_range: looseString(raw['reference_range']),
    abnormal_flag: looseString(raw['abnormal_flag']),
    page_no: finiteNumber(raw['page_no']),
    bbox: bboxFrom(raw['bbox']),
    match_confidence: finiteNumber(raw['match_confidence']),
    supported: raw['supported'] !== false,
    citation: id !== '' ? (citationsByFact.get(id) ?? null) : null,
  };
}

/**
 * Normalize the `GET /v1/documents/{id}` body. Never throws: garbage in →
 * a safe empty detail (status '', zero facts) carrying the requested id, so
 * page URLs stay constructible. Facts are joined to their document citations
 * on `field_or_chunk_id` here, once, so the UI renders one shape.
 */
export function documentDetailFrom(payload: unknown, requestedId: string): DocumentDetail {
  if (!isRecord(payload)) {
    return {
      document_id: requestedId,
      patient_id: 0,
      status: '',
      doc_type: '',
      page_count: null,
      facts: [],
    };
  }

  const citationsByFact = new Map<string, DocumentCitation>();
  const rawCitations = payload['citations'];
  if (Array.isArray(rawCitations)) {
    for (const raw of rawCitations) {
      const citation = citationFrom(raw);
      if (citation !== null && !citationsByFact.has(citation.field_or_chunk_id)) {
        citationsByFact.set(citation.field_or_chunk_id, citation);
      }
    }
  }

  const extraction = payload['extraction'];
  const rawFacts = isRecord(extraction) ? extraction['facts'] : undefined;
  const facts: ExtractedFact[] = [];
  if (Array.isArray(rawFacts)) {
    for (const raw of rawFacts) {
      const fact = factFrom(raw, citationsByFact);
      if (fact !== null) {
        facts.push(fact);
      }
    }
  }

  const documentId = idString(payload['document_id']);
  return {
    document_id: documentId !== '' ? documentId : requestedId,
    patient_id: looseId(payload['patient_id']),
    status: looseString(payload['status']),
    doc_type: looseString(payload['doc_type']),
    page_count: finiteNumber(payload['page_count']),
    facts,
  };
}

/**
 * Read one uploaded document's ingestion status plus the latest extraction's
 * facts and citations — `GET /v1/documents/{document_id}`. Identity follows
 * the app's auth-mode contract: the session cookie rides along via
 * `credentials: 'include'` (smart mode), and `clinicianId`, when provided, is
 * asserted as the `clinician_id` query param (required in disabled mode,
 * checked-for-match in smart mode — same convention as the observations GET).
 *
 * Transport failures throw a typed `ApiError` (so a poller can retry);
 * a malformed *body* normalizes to a safe empty detail and never throws.
 */
export async function getDocument(
  documentId: string,
  clinicianId?: number,
  base: string = resolveApiBase(),
): Promise<DocumentDetail> {
  const query =
    clinicianId !== undefined ? `?clinician_id=${encodeURIComponent(String(clinicianId))}` : '';
  let response: Response;
  try {
    response = await fetch(`${base}/v1/documents/${encodeURIComponent(documentId)}${query}`, {
      credentials: 'include',
      headers: { Accept: 'application/json' },
    });
  } catch {
    throw new ApiError('Could not reach the record service');
  }
  if (!response.ok) {
    throw new ApiError(`Record service replied ${response.status}`, response.status);
  }
  let payload: unknown;
  try {
    payload = (await response.json()) as unknown;
  } catch {
    throw new ApiError('Record service returned malformed JSON');
  }
  return documentDetailFrom(payload, documentId);
}

function acceptedFrom(payload: unknown): DocumentAccepted {
  if (typeof payload === 'object' && payload !== null) {
    const raw = payload as Record<string, unknown>;
    const id = raw['document_id'];
    if (typeof id === 'string' && id !== '') {
      return {
        document_id: id,
        status: typeof raw['status'] === 'string' ? raw['status'] : 'processing',
        correlation_id:
          typeof raw['correlation_id'] === 'string' ? raw['correlation_id'] : null,
      };
    }
  }
  throw new ApiError('Record service returned malformed JSON');
}

/**
 * Upload one source document for async extraction. The service replies
 * `202 {document_id, status, correlation_id}`; anything else is a typed
 * `ApiError` (status null when the service is unreachable).
 *
 * Identity follows the same auth-mode contract as `getDocument` above: the
 * session cookie rides along via `credentials: 'include'` (smart mode), and
 * `clinicianId`, when provided, is asserted explicitly — required in disabled
 * mode, checked-for-match in smart mode. The one difference is the carrier:
 * `getDocument` is a GET so it asserts on the query string, whereas
 * `POST /v1/documents` declares `clinician_id` as an optional *form* field
 * (copilot/api/routes/documents.py), so it belongs in the multipart body.
 * Omitting it entirely is what made a disabled-mode browser upload 400
 * ("clinician_id is required") while the query-string GET beside it worked.
 */
export async function uploadDocument(
  file: File,
  patientId: number,
  docType: DocType = DEFAULT_DOC_TYPE,
  clinicianId?: number,
  base: string = resolveApiBase(),
): Promise<DocumentAccepted> {
  const body = new FormData();
  body.append('file', file, file.name);
  body.append('patient_id', String(patientId));
  body.append('doc_type', docType);
  if (clinicianId !== undefined) {
    body.append('clinician_id', String(clinicianId));
  }

  const headers: Record<string, string> = { Accept: 'application/json' };
  const token = getCsrfToken();
  if (token !== null) {
    headers['X-CSRF-Token'] = token;
  }

  let response: Response;
  try {
    response = await fetch(`${base}/v1/documents`, {
      method: 'POST',
      credentials: 'include',
      headers,
      body,
    });
  } catch {
    throw new ApiError('Could not reach the record service');
  }
  if (!response.ok) {
    throw new ApiError(`Record service replied ${response.status}`, response.status);
  }
  let payload: unknown;
  try {
    payload = (await response.json()) as unknown;
  } catch {
    throw new ApiError('Record service returned malformed JSON');
  }
  return acceptedFrom(payload);
}
