/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Base URL of the live Co-Pilot API. Unset → the mock adapter serves the demo cohort. */
  readonly VITE_API_BASE_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
