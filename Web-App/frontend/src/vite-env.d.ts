/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** API address, default "/api" (same origin). Build-time. */
  readonly VITE_API_BASE_URL?: string;
  /** "true" = answer from src/mocks/ instead of the API. Build-time. */
  readonly VITE_USE_MOCKS?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
