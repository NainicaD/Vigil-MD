import { describe, expect, it } from "vitest";
import { readConfig } from "./config";

describe("readConfig", () => {
  it("defaults to /api on the same origin, mocks off", () => {
    expect(readConfig({})).toEqual({ apiBaseUrl: "/api", useMocks: false, devTools: false, requestTimeoutMs: 30_000 });
  });

  it("reads NEXT_PUBLIC_API_BASE_URL and drops a trailing slash", () => {
    expect(readConfig({ NEXT_PUBLIC_API_BASE_URL: "https://example.org/api/" }).apiBaseUrl).toBe(
      "https://example.org/api",
    );
  });

  it("turns the developer tools on only for exactly 'true'", () => {
    expect(readConfig({ NEXT_PUBLIC_DEV_TOOLS: "true" }).devTools).toBe(true);
    expect(readConfig({ NEXT_PUBLIC_DEV_TOOLS: "1" }).devTools).toBe(false);
    expect(readConfig({}).devTools).toBe(false);
  });

  it("turns mocks on only for exactly 'true'", () => {
    expect(readConfig({ NEXT_PUBLIC_USE_MOCKS: "true" }).useMocks).toBe(true);
    expect(readConfig({ NEXT_PUBLIC_USE_MOCKS: "TRUE" }).useMocks).toBe(false);
    expect(readConfig({ NEXT_PUBLIC_USE_MOCKS: "1" }).useMocks).toBe(false);
    expect(readConfig({ NEXT_PUBLIC_USE_MOCKS: "false" }).useMocks).toBe(false);
  });
});
