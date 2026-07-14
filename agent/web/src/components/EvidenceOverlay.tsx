/**
 * Evidence bounding-box overlay — a hand-rolled SVG over a rendered document
 * page image, no overlay library (the same idiom as MetricChart's hand-rolled
 * chart). Document citations carry a normalized `[x, y, w, h]` bbox; this
 * component draws that region on the page so the physician sees exactly where
 * the quoted value came from.
 *
 * Geometry: the SVG shares the image's pixel coordinate system — its viewBox
 * is `0 0 imageWidth imageHeight` and it is stretched across the exact same
 * box as the <img> (absolute inset 0, preserveAspectRatio="none"). Because
 * the image fills that box at its own intrinsic aspect ratio, a normalized
 * bbox scaled by the intrinsic dimensions lands on the right pixels at every
 * rendered size — responsive scaling for free, with no resize listeners.
 */

import { bboxToRect, type OverlayRect as OverlayRectShape } from '../overlayGeometry';

export { bboxToRect } from '../overlayGeometry';
export type { OverlayRect } from '../overlayGeometry';

/** One highlight region: a normalized bbox plus an optional accessible label. */
export interface EvidenceBox {
  /** Normalized [x, y, w, h], each component in 0–1. */
  bbox: readonly number[];
  label?: string;
}

export function EvidenceOverlay({
  src,
  alt,
  imageWidth,
  imageHeight,
  boxes,
}: {
  /** The rendered page image (GET /v1/documents/{id}/pages/{n}). */
  src: string;
  alt: string;
  /** Intrinsic pixel dimensions of the page image. */
  imageWidth: number;
  imageHeight: number;
  boxes: EvidenceBox[];
}): JSX.Element {
  const rects: { rect: OverlayRectShape; label: string | undefined }[] = [];
  for (const box of boxes) {
    const rect = bboxToRect(box.bbox, imageWidth, imageHeight);
    if (rect !== null) {
      rects.push({ rect, label: box.label });
    }
  }

  return (
    <div className="evidence-overlay">
      <img
        className="evidence-page"
        src={src}
        alt={alt}
        width={imageWidth}
        height={imageHeight}
      />
      <svg
        className="evidence-svg"
        viewBox={`0 0 ${imageWidth} ${imageHeight}`}
        preserveAspectRatio="none"
        aria-hidden="true"
        focusable="false"
      >
        {rects.map(({ rect, label }, i) => (
          <rect
            key={`${rect.x}-${rect.y}-${i}`}
            className="evidence-box"
            x={rect.x}
            y={rect.y}
            width={rect.width}
            height={rect.height}
          >
            {label !== undefined ? <title>{label}</title> : null}
          </rect>
        ))}
      </svg>
    </div>
  );
}
