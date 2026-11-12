/**
 * The developer page's session log: every API call made in this browser tab, with its result,
 * time taken and BigQuery cost. Kept in memory only (gone on reload), at most MAX_CALLS, never
 * sent anywhere. Only loaded in builds with NEXT_PUBLIC_DEV_TOOLS=true (see api/client.ts).
 */
import { useSyncExternalStore } from "react";
import type { Api } from "../api/client";
import { ApiMessageError, NotFoundError, ServiceUnavailableError, isAbortError } from "../api/errors";

export const MAX_CALLS = 200;

export type CallName = "search" | "entity" | "answer" | "health";

export interface CallRecord {
  id: number;
  /** When the call started (ms since epoch). */
  at: number;
  call: CallName;
  /** What was asked: the search text, the entity, or the start of the question. */
  detail: string;
  outcome: "ok" | "failed" | "cancelled";
  /** e.g. "12 hits", "answered", "HTTP 429 rate_limited", "timeout". */
  result: string;
  ms: number;
  /** BigQuery GB billed, when the API reports it. */
  gb: number | null;
}

let calls: CallRecord[] = [];
let nextId = 1;
const listeners = new Set<() => void>();

function emit() {
  for (const listener of listeners) listener();
}

export function recordCall(record: Omit<CallRecord, "id">): void {
  calls = [{ ...record, id: nextId++ }, ...calls].slice(0, MAX_CALLS);
  emit();
}

export function clearCalls(): void {
  calls = [];
  emit();
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

const getCalls = () => calls;

/** The log, newest first; re-renders when a call is added. */
export function useCallLog(): CallRecord[] {
  return useSyncExternalStore(subscribe, getCalls, getCalls);
}

export interface CallSummary {
  total: number;
  failed: number;
  cancelled: number;
  avgMs: number | null;
  maxMs: number | null;
  totalGb: number;
}

export function summarize(records: CallRecord[]): CallSummary {
  const done = records.filter((r) => r.outcome !== "cancelled");
  const times = done.map((r) => r.ms);
  return {
    total: records.length,
    failed: records.filter((r) => r.outcome === "failed").length,
    cancelled: records.length - done.length,
    avgMs: times.length ? Math.round(times.reduce((a, b) => a + b, 0) / times.length) : null,
    maxMs: times.length ? Math.max(...times) : null,
    totalGb: records.reduce((sum, r) => sum + (r.gb ?? 0), 0),
  };
}

/** A short description of a failed call. */
export function describeFailure(error: unknown): string {
  if (error instanceof ApiMessageError) return `HTTP ${error.httpStatus}${error.status ? ` ${error.status}` : ""}`;
  if (error instanceof NotFoundError) return "HTTP 404";
  if (error instanceof ServiceUnavailableError) return error.reason === "server" ? "server error" : error.reason;
  return "error";
}

function spentGb(response: unknown): number | null {
  const spent = (response as { budget_gb?: { spent?: unknown } | null } | null)?.budget_gb?.spent;
  return typeof spent === "number" ? spent : null;
}

const short = (text: string, max = 60) => (text.length > max ? `${text.slice(0, max - 1)}…` : text);

/** The same Api, with every call recorded in the log. */
export function withCallLog(api: Api): Api {
  async function logged<T>(call: CallName, detail: string, run: () => Promise<T>, describe: (r: T) => string): Promise<T> {
    const at = Date.now();
    const start = performance.now();
    const ms = () => Math.round(performance.now() - start);
    try {
      const response = await run();
      recordCall({ at, call, detail, outcome: "ok", result: describe(response), ms: ms(), gb: spentGb(response) });
      return response;
    } catch (error) {
      const cancelled = isAbortError(error);
      recordCall({
        at,
        call,
        detail,
        outcome: cancelled ? "cancelled" : "failed",
        result: cancelled ? "cancelled" : describeFailure(error),
        ms: ms(),
        gb: null,
      });
      throw error;
    }
  }

  return {
    search: (query, options) =>
      logged("search", `"${short(query)}"`, () => api.search(query, options), (r) => `${r.hits.length} hits`),
    entity: (kind, entityId, options) =>
      logged("entity", `${kind} ${entityId}`, () => api.entity(kind, entityId, options), (r) =>
        r.entity.found ? "found" : "no data",
      ),
    answer: (question, entity, options) =>
      logged(
        "answer",
        `"${short(question)}"${entity ? ` about ${entity.entity_type} ${entity.entity_id}` : ""}`,
        () => api.answer(question, entity, options),
        (r) => r.status,
      ),
    health: (options) => logged("health", "", () => api.health(options), (r) => r.status),
  };
}
