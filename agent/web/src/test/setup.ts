/**
 * Vitest setup — runs before every test file (see vitest.config.ts).
 * Unmounts rendered trees between tests and fills the two browser APIs
 * react-aria-components touches that jsdom does not implement.
 */

import { cleanup } from '@testing-library/react';
import { afterEach } from 'vitest';

afterEach(() => {
  cleanup();
});

// react-aria overlay positioning observes element resize; jsdom has no
// ResizeObserver. A no-op stand-in keeps popover-bearing components mountable.
class ResizeObserverStub {
  public observe(): void {
    /* no-op */
  }

  public unobserve(): void {
    /* no-op */
  }

  public disconnect(): void {
    /* no-op */
  }
}

if (typeof globalThis.ResizeObserver === 'undefined') {
  globalThis.ResizeObserver = ResizeObserverStub as unknown as typeof ResizeObserver;
}

// jsdom implements matchMedia only partially in some versions; react-aria
// queries it for reduced-motion. Provide a minimal implementation if absent.
if (typeof window.matchMedia !== 'function') {
  window.matchMedia = ((query: string) =>
    ({
      matches: false,
      media: query,
      onchange: null,
      addListener: () => undefined,
      removeListener: () => undefined,
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
      dispatchEvent: () => false,
    }) as MediaQueryList) as typeof window.matchMedia;
}
