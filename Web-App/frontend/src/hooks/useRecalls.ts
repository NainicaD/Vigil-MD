import { useCallback, useEffect, useState } from "react";
import { api } from "../api/client";
import { failureMessage, isAbortError } from "../api/errors";
import type { RecallPage } from "../api/export";

export type RecallsState =
  | { status: "loading"; previous?: RecallPage }
  | { status: "done"; data: RecallPage }
  | { status: "error"; message: string };

type Result = { key: string; attempt: number } & Exclude<RecallsState, { status: "loading" }>;

/** One page of the recall finder for the given filters (a query string) and page number. */
export function useRecalls(filters: string, page: number): { state: RecallsState; retry: () => void } {
  const key = `${filters}#${page}`;
  const [attempt, setAttempt] = useState(0);
  const [result, setResult] = useState<Result | null>(null);

  useEffect(() => {
    const ctrl = new AbortController();
    api
      .recalls(new URLSearchParams(filters), page, { signal: ctrl.signal })
      .then((data) => setResult({ key, attempt, status: "done", data }))
      .catch((error: unknown) => {
        if (!isAbortError(error)) setResult({ key, attempt, status: "error", message: failureMessage(error) });
      });
    return () => ctrl.abort();
  }, [filters, page, key, attempt]);

  const retry = useCallback(() => setAttempt((n) => n + 1), []);

  if (!result || result.key !== key || result.attempt !== attempt) {
    return { state: { status: "loading", previous: result?.status === "done" ? result.data : undefined }, retry };
  }
  return { state: result.status === "done" ? { status: "done", data: result.data } : result, retry };
}
