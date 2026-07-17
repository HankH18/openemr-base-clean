/**
 * Resolves the Co-Pilot API base URL from the build-time env, applying the
 * exact normalization createApi() uses: a trailing-slash-stripped origin, or
 * '' for same-origin (VITE_API_BASE_URL=/). Shared by the API adapter, the
 * auth hook, the login gate, and the fetch layer so the /v1/auth/* URLs
 * (status, login, logout, 401-redirect) always line up with the data calls.
 */
export function resolveApiBase(): string {
  const base = import.meta.env.VITE_API_BASE_URL;
  if (typeof base === 'string' && base.trim() !== '') {
    return base.trim().replace(/\/+$/, '');
  }
  return '';
}
