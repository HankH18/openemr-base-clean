import { render } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { bboxToRect, EvidenceOverlay } from './EvidenceOverlay';

describe('EvidenceOverlay bbox geometry', () => {
  it('maps a normalized bbox to SVG rect coordinates for the image dimensions', () => {
    const rect = bboxToRect([0.25, 0.5, 0.5, 0.25], 1000, 800);
    expect(rect).toEqual({ x: 250, y: 400, width: 500, height: 200 });
  });

  it('scales the same normalized bbox correctly across different aspect ratios', () => {
    // US-letter page rasterized at 612x792 (portrait)…
    const portrait = bboxToRect([0.1, 0.2, 0.3, 0.4], 612, 792);
    expect(portrait).not.toBeNull();
    expect(portrait?.x).toBeCloseTo(61.2, 5);
    expect(portrait?.y).toBeCloseTo(158.4, 5);
    expect(portrait?.width).toBeCloseTo(183.6, 5);
    expect(portrait?.height).toBeCloseTo(316.8, 5);

    // …and the same box on a landscape scan: x scales by width, y by height.
    const landscape = bboxToRect([0.1, 0.2, 0.3, 0.4], 1600, 900);
    expect(landscape).not.toBeNull();
    expect(landscape?.x).toBeCloseTo(160, 5);
    expect(landscape?.y).toBeCloseTo(180, 5);
    expect(landscape?.width).toBeCloseTo(480, 5);
    expect(landscape?.height).toBeCloseTo(360, 5);
  });

  it('clamps a bounding box that sticks out past the page to the page bounds', () => {
    const rect = bboxToRect([0.8, -0.1, 0.5, 0.3], 1000, 1000);
    expect(rect).not.toBeNull();
    expect(rect?.x).toBeCloseTo(800, 5);
    expect(rect?.y).toBeCloseTo(0, 5);
    expect(rect?.width).toBeCloseTo(200, 5);
    expect(rect?.height).toBeCloseTo(200, 5);
  });

  it('returns null for malformed bboxes instead of drawing a wrong box', () => {
    expect(bboxToRect([0.1, 0.2, 0.3], 1000, 800)).toBeNull(); // wrong arity
    expect(bboxToRect([0.1, 0.2, 0.3, Number.NaN], 1000, 800)).toBeNull(); // non-finite
    expect(bboxToRect([1.2, 1.2, 0.5, 0.5], 1000, 800)).toBeNull(); // fully off-page
    expect(bboxToRect([0.1, 0.2, 0, 0.3], 1000, 800)).toBeNull(); // zero width
    expect(bboxToRect([0.1, 0.2, 0.3, 0.4], 0, 800)).toBeNull(); // unusable dims
  });

  it('renders one SVG rect per valid bounding box positioned over the page image', () => {
    const { container } = render(
      <EvidenceOverlay
        src="/pages/doc-19/3.png"
        alt="Lab report, page 3"
        imageWidth={1000}
        imageHeight={800}
        boxes={[
          { bbox: [0.25, 0.5, 0.5, 0.25], label: 'Hemoglobin 9.1 g/dL' },
          { bbox: [0.1, 0.2, 0.3], label: 'malformed — must be skipped' },
        ]}
      />,
    );

    const svg = container.querySelector('svg');
    expect(svg).not.toBeNull();
    expect(svg?.getAttribute('viewBox')).toBe('0 0 1000 800');
    expect(svg?.getAttribute('preserveAspectRatio')).toBe('none');

    const rects = container.querySelectorAll('rect');
    expect(rects.length).toBe(1);
    const rect = rects[0];
    expect(rect).toBeDefined();
    expect(Number.parseFloat(rect?.getAttribute('x') ?? '')).toBeCloseTo(250, 5);
    expect(Number.parseFloat(rect?.getAttribute('y') ?? '')).toBeCloseTo(400, 5);
    expect(Number.parseFloat(rect?.getAttribute('width') ?? '')).toBeCloseTo(500, 5);
    expect(Number.parseFloat(rect?.getAttribute('height') ?? '')).toBeCloseTo(200, 5);

    const image = container.querySelector('img');
    expect(image?.getAttribute('src')).toBe('/pages/doc-19/3.png');
    expect(image?.getAttribute('alt')).toBe('Lab report, page 3');
  });
});
