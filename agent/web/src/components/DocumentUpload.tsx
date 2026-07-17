import { useState } from 'react';
import { Button, FileTrigger, Radio, RadioGroup } from 'react-aria-components';
import {
  DEFAULT_DOC_TYPE,
  DOC_TYPES,
  DOC_TYPE_LABELS,
  isDocType,
  uploadDocument,
  type DocType,
} from '../api/documents';
import { ApiError, type DocumentAccepted } from '../api/types';

export type UploadFn = (file: File, docType: DocType) => Promise<DocumentAccepted>;

type Phase = 'idle' | 'uploading' | 'accepted' | 'error';

function failureMessage(error: unknown): string {
  if (error instanceof ApiError) {
    return error.status === null
      ? 'Could not reach the record service — the document was not ingested.'
      : `Upload failed — the record service replied ${error.status}.`;
  }
  return 'Upload failed — the document was not ingested.';
}

/**
 * Document upload control — a React Aria `FileTrigger` (keyboard/screen-reader
 * accessible file picking with no bare <input> styling fights) that posts the
 * chosen file as multipart form data to `POST /v1/documents` and narrates the
 * outcome inline: 202 → accepted-for-extraction, anything else → a plain
 * error. Extraction is async server-side, so "accepted" deliberately does not
 * claim the facts are ready.
 *
 * A segmented `RadioGroup` beside the trigger picks the document kind; the
 * selection is sent verbatim as `doc_type`. The choices are the backend's
 * closed `DocumentType` enum, typed end-to-end (`DocType`), so an invalid
 * value cannot be sent.
 *
 * `upload` is injectable (the App seam passes the API adapter's uploader, so
 * mock mode simulates ingestion); it defaults to the real multipart poster.
 * That default poster needs `clinicianId` for the same reason the seam does —
 * in disabled mode the service demands an asserted `clinician_id` and 400s
 * without one.
 */
export function DocumentUpload({
  patientId,
  clinicianId,
  initialDocType = DEFAULT_DOC_TYPE,
  upload,
  onAccepted,
}: {
  patientId: number;
  /**
   * Acting clinician, asserted as `clinician_id` on the upload. Only consulted
   * by the built-in poster below — when `upload` is injected (as the App seam
   * always does) identity is already bound into that callback.
   */
  clinicianId?: number;
  /** Pre-selected document kind; the physician can change it before uploading. */
  initialDocType?: DocType;
  upload?: UploadFn;
  onAccepted?: (accepted: DocumentAccepted) => void;
}): JSX.Element {
  const [phase, setPhase] = useState<Phase>('idle');
  const [message, setMessage] = useState('');
  const [docType, setDocType] = useState<DocType>(initialDocType);

  const doUpload: UploadFn =
    upload ?? ((file, type) => uploadDocument(file, patientId, type, clinicianId));

  const handleSelect = (files: FileList | null): void => {
    const file = files?.[0];
    if (file === undefined || phase === 'uploading') {
      return;
    }
    setPhase('uploading');
    setMessage(`Uploading ${file.name}…`);
    doUpload(file, docType)
      .then((accepted) => {
        setPhase('accepted');
        setMessage(`${file.name} accepted — extracting (${accepted.document_id}).`);
        onAccepted?.(accepted);
      })
      .catch((error: unknown) => {
        setPhase('error');
        setMessage(failureMessage(error));
      });
  };

  return (
    <div className="doc-upload" data-phase={phase}>
      <RadioGroup
        className="doc-type-group"
        aria-label="Document type"
        orientation="horizontal"
        value={docType}
        onChange={(value) => {
          if (isDocType(value)) {
            setDocType(value);
          }
        }}
        isDisabled={phase === 'uploading'}
      >
        {DOC_TYPES.map((type) => (
          <Radio key={type} className="doc-type-chip" value={type}>
            {DOC_TYPE_LABELS[type]}
          </Radio>
        ))}
      </RadioGroup>
      <FileTrigger
        acceptedFileTypes={['application/pdf', 'image/png', 'image/jpeg', 'image/tiff']}
        onSelect={handleSelect}
      >
        <Button className="btn doc-upload-btn" isDisabled={phase === 'uploading'}>
          {phase === 'uploading' ? 'Uploading…' : 'Upload document'}
        </Button>
      </FileTrigger>
      {message !== '' ? (
        <p
          className={`doc-upload-status doc-upload-status--${phase}`}
          role={phase === 'error' ? 'alert' : 'status'}
        >
          {message}
        </p>
      ) : null}
    </div>
  );
}
