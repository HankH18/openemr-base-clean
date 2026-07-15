/**
 * Document ingestion — `POST /v1/documents` (multipart). Kept beside the JSON
 * fetch layer (http.ts) rather than inside it because multipart must NOT set
 * a Content-Type header: the browser writes the boundary itself.
 */

import { resolveApiBase } from './base';
import { getCsrfToken } from './session';
import { ApiError, type DocumentAccepted } from './types';

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
 */
export async function uploadDocument(
  file: File,
  patientId: number,
  docType: DocType = DEFAULT_DOC_TYPE,
  base: string = resolveApiBase(),
): Promise<DocumentAccepted> {
  const body = new FormData();
  body.append('file', file, file.name);
  body.append('patient_id', String(patientId));
  body.append('doc_type', docType);

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
