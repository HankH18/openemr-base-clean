/**
 * A correlation id that also works in an INSECURE context.
 *
 * `crypto.randomUUID()` (and the whole Web Crypto API) is only exposed in a
 * secure context — HTTPS or localhost. On a plain-HTTP deploy (e.g. a bare-IP
 * demo) it is `undefined`, so calling it throws and would fail the request
 * before it is even sent. Prefer it when available, else fall back to a random
 * token. Either form satisfies the server's CorrelationId constraint
 * (8-64 chars of [A-Za-z0-9_-]).
 */
export function newCorrelationId(): string {
  const c: Crypto | undefined = globalThis.crypto;
  if (c && typeof c.randomUUID === 'function') {
    return c.randomUUID();
  }
  const rand = (): string => Math.random().toString(36).slice(2, 12);
  return `cid-${rand()}${rand()}`;
}
