import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { DocumentEvidence } from './DocumentEvidence';

const SRC = '/v1/documents/doc-19/pages/3';
const ALT = 'Uploaded document doc-19, page 3 — cited region highlighted';
const BOXES = [{ bbox: [0.25, 0.5, 0.5, 0.25], label: 'Hemoglobin 9.1 g/dL' }];

/** jsdom never decodes images, so intrinsic dimensions are stubbed per test. */
function setIntrinsicSize(image: HTMLImageElement, width: number, height: number): void {
  Object.defineProperty(image, 'naturalWidth', { value: width, configurable: true });
  Object.defineProperty(image, 'naturalHeight', { value: height, configurable: true });
}

function probeImage(container: HTMLElement): HTMLImageElement {
  const probe = container.querySelector<HTMLImageElement>('img.evidence-probe');
  expect(probe).not.toBeNull();
  if (probe === null) {
    throw new Error('probe image not rendered');
  }
  return probe;
}

describe('DocumentEvidence', () => {
  it('shows a quiet placeholder while the page image is loading', () => {
    const { container } = render(<DocumentEvidence src={SRC} alt={ALT} boxes={BOXES} />);

    expect(screen.getByText(/loading cited page/i)).toBeTruthy();
    expect(container.querySelector('svg')).toBeNull();
    expect(probeImage(container).getAttribute('src')).toBe(SRC);
  });

  it('renders the EvidenceOverlay with the intrinsic dimensions once the image loads', () => {
    const { container } = render(<DocumentEvidence src={SRC} alt={ALT} boxes={BOXES} />);

    const probe = probeImage(container);
    setIntrinsicSize(probe, 1000, 800);
    fireEvent.load(probe);

    // Overlay geometry is driven by the probed naturalWidth/naturalHeight.
    const svg = container.querySelector('svg.evidence-svg');
    expect(svg?.getAttribute('viewBox')).toBe('0 0 1000 800');

    const rect = container.querySelector('rect.evidence-box');
    expect(rect).not.toBeNull();
    expect(Number.parseFloat(rect?.getAttribute('x') ?? '')).toBeCloseTo(250, 5);
    expect(Number.parseFloat(rect?.getAttribute('y') ?? '')).toBeCloseTo(400, 5);
    expect(Number.parseFloat(rect?.getAttribute('width') ?? '')).toBeCloseTo(500, 5);
    expect(Number.parseFloat(rect?.getAttribute('height') ?? '')).toBeCloseTo(200, 5);

    const page = container.querySelector('img.evidence-page');
    expect(page?.getAttribute('src')).toBe(SRC);
    expect(page?.getAttribute('alt')).toBe(ALT);

    expect(container.querySelector('.evidence-loading')).toBeNull();
  });

  it('renders nothing when the page image fails to load — never a broken image', () => {
    const { container } = render(<DocumentEvidence src={SRC} alt={ALT} boxes={BOXES} />);

    fireEvent.error(probeImage(container));

    expect(container.querySelector('img')).toBeNull();
    expect(container.querySelector('svg')).toBeNull();
    expect(container.textContent).toBe('');
  });

  it('treats a zero-dimension load as a failure', () => {
    const { container } = render(<DocumentEvidence src={SRC} alt={ALT} boxes={BOXES} />);

    const probe = probeImage(container);
    setIntrinsicSize(probe, 0, 0);
    fireEvent.load(probe);

    expect(container.querySelector('img')).toBeNull();
    expect(container.querySelector('svg')).toBeNull();
  });
});
