import { useState } from 'react';
import { Button, FileTrigger } from 'react-aria-components';
import { DEFAULT_DOC_TYPE, uploadDocument } from '../api/documents';
import { ApiError, type DocumentAccepted } from '../api/types';

export type UploadFn = (file: File) => Promise<DocumentAccepted>;

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
 * `upload` is injectable (the App seam passes the API adapter's uploader, so
 * mock mode simulates ingestion); it defaults to the real multipart poster.
 */
export function DocumentUpload({
  patientId,
  docType = DEFAULT_DOC_TYPE,
  upload,
  onAccepted,
}: {
  patientId: number;
  docType?: string;
  upload?: UploadFn;
  onAccepted?: (accepted: DocumentAccepted) => void;
}): JSX.Element {
  const [phase, setPhase] = useState<Phase>('idle');
  const [message, setMessage] = useState('');

  const doUpload: UploadFn = upload ?? ((file) => uploadDocument(file, patientId, docType));

  const handleSelect = (files: FileList | null): void => {
    const file = files?.[0];
    if (file === undefined || phase === 'uploading') {
      return;
    }
    setPhase('uploading');
    setMessage(`Uploading ${file.name}…`);
    doUpload(file)
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
