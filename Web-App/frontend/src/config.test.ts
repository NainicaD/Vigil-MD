import { describe, expect, it } from "vitest";
import { readConfig } from "./config";

describe("readConfig", () => {
  it("defaults to /api on the same origin, mocks off", () => {
    expect(readConfig({})).toEqual({ apiBaseUrl: "/api", useMocks: false, requestTimeoutMs: 30_000 });
  });

  it("reads VITE_API_BASE_URL and drops a trailing slash", () => {
    expect(readConfig({ VITE_API_BASE_URL: "https://example.org/api/" }).apiBaseUrl).toBe(
      "https://example.org/api",
    );
  });

  it("turns mocks on only for exactly 'true'", () => {
    expect(readConfig({ VITE_USE_MOCKS: "true" }).useMocks).toBe(true);
    expect(readConfig({ VITE_USE_MOCKS: "TRUE" }).useMocks).toBe(false);
    expect(readConfig({ VITE_USE_MOCKS: "1" }).useMocks).toBe(false);
    expect(readConfig({ VITE_USE_MOCKS: "false" }).useMocks).toBe(false);
  });
});
