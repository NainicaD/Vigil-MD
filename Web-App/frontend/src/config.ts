/**
 * Build-time settings (handover §1). Read once here; the rest of the app imports `config`.
 * No addresses are hard-coded: the API defaults to "/api" on the same origin.
 */

export interface AppConfig {
  apiBaseUrl: string;
  useMocks: boolean;
  /** The developer page and the call log are built in (never in a public build). */
  devTools: boolean;
  /** Give up on a request after this long and show the "Can't reach the service" message. */
  requestTimeoutMs: number;
}

export function readConfig(env: Partial<Record<string, string | undefined>>): AppConfig {
  const base = (env.NEXT_PUBLIC_API_BASE_URL ?? "").trim() || "/api";
  return {
    apiBaseUrl: base.replace(/\/+$/, ""), // no trailing slash
    // Exactly "true", the same test as api/client.ts (which lets the build drop the mocks).
    useMocks: env.NEXT_PUBLIC_USE_MOCKS === "true",
    devTools: env.NEXT_PUBLIC_DEV_TOOLS === "true",
    requestTimeoutMs: 30_000,
  };
}

// Written out in full: Next.js replaces each `process.env.NEXT_PUBLIC_…` with its value at
// build time (next.config.ts always defines both).
export const config: AppConfig = readConfig({
  NEXT_PUBLIC_API_BASE_URL: process.env.NEXT_PUBLIC_API_BASE_URL,
  NEXT_PUBLIC_USE_MOCKS: process.env.NEXT_PUBLIC_USE_MOCKS,
  NEXT_PUBLIC_DEV_TOOLS: process.env.NEXT_PUBLIC_DEV_TOOLS,
});

/** About this build, for the developer page. */
export const buildInfo = {
  version: process.env.NEXT_PUBLIC_APP_VERSION || null,
  builtAt: process.env.NEXT_PUBLIC_BUILT_AT || null,
};
