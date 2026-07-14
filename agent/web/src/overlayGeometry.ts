/**
 * Pure bbox → SVG-rect geometry for the evidence overlay. Kept dependency-
 * free and component-free so the mapping is directly unit-testable.
 */

/** Pixel-space rect in the page image's coordinate system. */
export interface OverlayRect {
  x: number;
  y: number;
  width: number;
  height: number;
}

function clamp01(value: number): number {
  return Math.min(1, Math.max(0, value));
}

/**
 * Map a normalized `[x, y, w, h]` bounding box (each component in 0–1,
 * origin top-left) to pixel rect coordinates for a page image of
 * `imageWidth` × `imageHeight`.
 *
 * Fail-safe: returns `null` — draw nothing — rather than a wrong box when the
 * bbox is malformed (wrong arity, non-finite parts) or the image dimensions
 * are unusable. Boxes that stick out past the page are clamped to the page
 * bounds; a box entirely outside (zero visible area) is `null`.
 */
export function bboxToRect(
  bbox: readonly number[],
  imageWidth: number,
  imageHeight: number,
): OverlayRect | null {
  if (bbox.length !== 4) {
    return null;
  }
  const [nx, ny, nw, nh] = bbox;
  if (nx === undefined || ny === undefined || nw === undefined || nh === undefined) {
    return null;
  }
  if (![nx, ny, nw, nh].every(Number.isFinite)) {
    return null;
  }
  if (!Number.isFinite(imageWidth) || !Number.isFinite(imageHeight)) {
    return null;
  }
  if (imageWidth <= 0 || imageHeight <= 0) {
    return null;
  }

  const x0 = clamp01(nx);
  const y0 = clamp01(ny);
  const x1 = clamp01(nx + nw);
  const y1 = clamp01(ny + nh);
  const w = x1 - x0;
  const h = y1 - y0;
  if (w <= 0 || h <= 0) {
    return null;
  }

  return {
    x: x0 * imageWidth,
    y: y0 * imageHeight,
    width: w * imageWidth,
    height: h * imageHeight,
  };
}
