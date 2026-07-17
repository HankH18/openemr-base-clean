/**
 * Document-citation evidence figure — loads the rendered page image for a
 * cited document page, reads its intrinsic pixel dimensions, then hands off
 * to `EvidenceOverlay`, whose bbox geometry requires those dimensions up
 * front (its SVG viewBox is the image's pixel coordinate system).
 *
 * The image is probed with a hidden <img> so `naturalWidth`/`naturalHeight`
 * are known before the overlay mounts; the second <img> EvidenceOverlay
 * renders comes straight from cache. States:
 *
 *   loading → a quiet placeholder while the page image streams in
 *   ready   → the page with the cited region boxed
 *   failed  → renders nothing, so the popover's text details remain the
 *             fallback view — never a broken image
 */

import { useEffect, useState, type SyntheticEvent } from 'react';
import { EvidenceOverlay, type EvidenceBox } from './EvidenceOverlay';

type LoadState =
  | { phase: 'loading' }
  | { phase: 'ready'; width: number; height: number }
  | { phase: 'failed' };

export function DocumentEvidence({
  src,
  alt,
  boxes,
}: {
  /** The rendered page image URL (GET /v1/documents/{id}/pages/{page_no}). */
  src: string;
  /** Accessible description of the page image. */
  alt: string;
  boxes: EvidenceBox[];
}): JSX.Element | null {
  const [state, setState] = useState<LoadState>({ phase: 'loading' });

  // A chip normally mounts with one fixed page URL, but stay correct if the
  // src ever changes: re-probe the new image from scratch.
  useEffect(() => {
    setState({ phase: 'loading' });
  }, [src]);

  if (state.phase === 'failed') {
    return null;
  }

  if (state.phase === 'ready') {
    return (
      <figure className="evidence-figure">
        <EvidenceOverlay
          src={src}
          alt={alt}
          imageWidth={state.width}
          imageHeight={state.height}
          boxes={boxes}
        />
      </figure>
    );
  }

  const handleLoad = (event: SyntheticEvent<HTMLImageElement>): void => {
    const image = event.currentTarget;
    if (image.naturalWidth > 0 && image.naturalHeight > 0) {
      setState({ phase: 'ready', width: image.naturalWidth, height: image.naturalHeight });
    } else {
      // A "loaded" image with no pixels is as unusable as a failed one.
      setState({ phase: 'failed' });
    }
  };

  return (
    <figure className="evidence-figure">
      <div className="evidence-loading">Loading cited page…</div>
      <img
        className="evidence-probe"
        src={src}
        alt=""
        aria-hidden="true"
        onLoad={handleLoad}
        onError={() => setState({ phase: 'failed' })}
      />
    </figure>
  );
}
