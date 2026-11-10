/**
 * Build-time settings (handover §1). Read once here; the rest of the app imports `config`.
 * No addresses are hard-coded: the API defaults to "/api" on the same origin.
 */

export interface AppConfig {
  apiBaseUrl: string;
  useMocks: boolean;
  /** Give up on a request after this long and show the "Can't reach the service" message. */
  requestTimeoutMs: number;
}

export function readConfig(env: Partial<Record<string, string | undefined>>): AppConfig {
  const base = (env.VITE_API_BASE_URL ?? "").trim() || "/api";
  return {
    apiBaseUrl: base.replace(/\/+$/, ""), // no trailing slash
    // Exactly "true", the same test as api/client.ts (which lets the build drop the mocks).
    useMocks: env.VITE_USE_MOCKS === "true",
    requestTimeoutMs: 30_000,
  };
}

export const config: AppConfig = readConfig(import.meta.env);
