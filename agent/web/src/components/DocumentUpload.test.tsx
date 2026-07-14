import { fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { DocumentUpload } from './DocumentUpload';

type FetchFn = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;

function jsonResponse(status: number, payload: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: () => Promise.resolve(payload),
  } as unknown as Response;
}

function accepted202(): Response {
  return jsonResponse(202, {
    document_id: 'doc-19',
    status: 'processing',
    correlation_id: 'corr-1',
  });
}

function selectFile(container: HTMLElement, file: File): void {
  const input = container.querySelector<HTMLInputElement>('input[type="file"]');
  expect(input).not.toBeNull();
  if (input === null) {
    return;
  }
  fireEvent.change(input, { target: { files: [file] } });
}

function labFile(): File {
  return new File(['%PDF-1.7 lab report'], 'lab-report.pdf', { type: 'application/pdf' });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('DocumentUpload FileTrigger upload flow', () => {
  it('posts the selected file as multipart form data to /v1/documents', async () => {
    const fetchMock = vi.fn<FetchFn>().mockResolvedValue(accepted202());
    vi.stubGlobal('fetch', fetchMock);

    const { container } = render(<DocumentUpload patientId={1003} />);
    selectFile(container, labFile());

    await screen.findByText(/accepted — extracting/i);

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const call = fetchMock.mock.calls[0];
    expect(call).toBeDefined();
    if (call === undefined) {
      return;
    }
    const [url, init] = call;
    expect(String(url)).toMatch(/\/v1\/documents$/);
    expect(init?.method).toBe('POST');

    const body = init?.body;
    expect(body).toBeInstanceOf(FormData);
    if (!(body instanceof FormData)) {
      return;
    }
    expect(body.get('patient_id')).toBe('1003');
    expect(body.get('doc_type')).toBe('intake_lab_report');
    const sent = body.get('file');
    expect(sent).toBeInstanceOf(File);
    if (sent instanceof File) {
      expect(sent.name).toBe('lab-report.pdf');
    }

    // Multipart must never carry an explicit Content-Type — the browser
    // writes the boundary itself.
    const headers = (init?.headers ?? {}) as Record<string, string>;
    expect(Object.keys(headers).map((h) => h.toLowerCase())).not.toContain('content-type');
  });

  it('shows the accepted state with the document id on a 202 response', async () => {
    vi.stubGlobal('fetch', vi.fn<FetchFn>().mockResolvedValue(accepted202()));

    const { container } = render(<DocumentUpload patientId={1003} />);
    selectFile(container, labFile());

    const status = await screen.findByRole('status');
    expect(status.textContent).toContain('doc-19');
    expect(container.querySelector('.doc-upload')?.getAttribute('data-phase')).toBe('accepted');
  });

  it('shows an error state when the service rejects the upload', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn<FetchFn>().mockResolvedValue(jsonResponse(500, { detail: 'boom' })),
    );

    const { container } = render(<DocumentUpload patientId={1003} />);
    selectFile(container, labFile());

    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toMatch(/replied 500/);
    expect(container.querySelector('.doc-upload')?.getAttribute('data-phase')).toBe('error');
  });

  it('shows an error state when the record service is unreachable', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn<FetchFn>().mockRejectedValue(new TypeError('network down')),
    );

    const { container } = render(<DocumentUpload patientId={1003} />);
    selectFile(container, labFile());

    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toMatch(/could not reach/i);
    expect(container.querySelector('.doc-upload')?.getAttribute('data-phase')).toBe('error');
  });

  it('routes through an injected upload function when one is provided', async () => {
    const fetchMock = vi.fn<FetchFn>();
    vi.stubGlobal('fetch', fetchMock);
    const upload = vi.fn().mockResolvedValue({
      document_id: 'mock-doc-1',
      status: 'processing',
      correlation_id: null,
    });

    const { container } = render(<DocumentUpload patientId={1003} upload={upload} />);
    selectFile(container, labFile());

    await screen.findByText(/accepted — extracting/i);
    expect(upload).toHaveBeenCalledTimes(1);
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
