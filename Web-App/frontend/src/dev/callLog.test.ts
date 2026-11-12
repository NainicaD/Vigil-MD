import { renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";
import type { Api } from "../api/client";
import { ApiMessageError, ServiceUnavailableError } from "../api/errors";
import type { AnswerResponse, EntityResponse } from "../api/types";
import { MAX_CALLS, clearCalls, recordCall, summarize, useCallLog, withCallLog, type CallRecord } from "./callLog";

/** The log as the developer page reads it (newest first). */
const current = () => renderHook(() => useCallLog()).result.current;

function fakeApi(over: Partial<Api> = {}): Api {
  return {
    search: async (query) => ({ query, hits: [] }),
    entity: async (kind, id) => ({ entity: { type: kind, id, found: true } }) as EntityResponse,
    answer: async () => ({ status: "answered", text: "", sections: [] }) as AnswerResponse,
    health: async () => ({ status: "ok" }),
    ...over,
  };
}

beforeEach(() => {
  clearCalls();
});

describe("withCallLog", () => {
  it("records a search with its hit count and time", async () => {
    const api = withCallLog(fakeApi({ search: async (q) => ({ query: q, hits: [{} as never, {} as never] }) }));
    await api.search("acme");
    const [call] = current();
    expect(call).toMatchObject({ call: "search", detail: '"acme"', outcome: "ok", result: "2 hits", gb: null });
    expect(call!.ms).toBeGreaterThanOrEqual(0);
  });

  it("records an answer with its status and BigQuery cost, the question shortened", async () => {
    const api = withCallLog(
      fakeApi({ answer: async () => ({ status: "answered", text: "", sections: [], budget_gb: { max: 0.5, spent: 0.042 } }) as AnswerResponse }),
    );
    await api.answer("x".repeat(100), { entity_type: "device", entity_id: "dev:1" });
    const [call] = current();
    expect(call).toMatchObject({ call: "answer", outcome: "ok", result: "answered", gb: 0.042 });
    expect(call!.detail).toMatch(/^"x{59}…" about device dev:1$/);
  });

  it("records a failure and passes the error on", async () => {
    const api = withCallLog(
      fakeApi({
        answer: async () => {
          throw new ApiMessageError(429, "rate_limited", "Too many questions in a minute.");
        },
      }),
    );
    await expect(api.answer("q", null)).rejects.toBeInstanceOf(ApiMessageError);
    expect(current()[0]).toMatchObject({ outcome: "failed", result: "HTTP 429 rate_limited" });
  });

  it("names timeouts and marks cancelled calls apart from failures", async () => {
    const api = withCallLog(
      fakeApi({
        health: async () => {
          throw new ServiceUnavailableError("timeout");
        },
        search: async () => {
          throw new DOMException("Aborted", "AbortError");
        },
      }),
    );
    await api.health().catch(() => {});
    await api.search("ac").catch(() => {});
    const [search, health] = current();
    expect(health).toMatchObject({ outcome: "failed", result: "timeout" });
    expect(search).toMatchObject({ outcome: "cancelled", result: "cancelled" });
  });
});

describe("the log", () => {
  const rec = (over: Partial<CallRecord> = {}) =>
    recordCall({ at: 0, call: "search", detail: "", outcome: "ok", result: "", ms: 10, gb: null, ...over });

  it("keeps the newest MAX_CALLS calls, newest first", () => {
    for (let i = 0; i < MAX_CALLS + 5; i++) rec({ detail: String(i) });
    const log = current();
    expect(log).toHaveLength(MAX_CALLS);
    expect(log[0]!.detail).toBe(String(MAX_CALLS + 4));
  });

  it("sums up calls, failures, times and cost; cancelled calls don't count for time", () => {
    rec({ ms: 100, gb: 0.04 });
    rec({ ms: 300, outcome: "failed" });
    rec({ ms: 5000, outcome: "cancelled" });
    expect(summarize(current())).toEqual({ total: 3, failed: 1, cancelled: 1, avgMs: 200, maxMs: 300, totalGb: 0.04 });
  });

  it("clears", () => {
    rec();
    clearCalls();
    expect(current()).toEqual([]);
  });
});
