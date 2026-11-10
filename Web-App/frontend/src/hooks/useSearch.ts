import { useEffect, useState } from "react";
import { api } from "../api/client";
import { SERVICE_UNAVAILABLE_MESSAGE, isAbortError } from "../api/errors";
import type { SearchHit } from "../api/types";

/** Search starts at 2 or more characters (handover §3). */
export const MIN_QUERY_LENGTH = 2;

export type SearchState =
  | { status: "idle" }
  | { status: "loading" }
  | { status: "done"; query: string; hits: SearchHit[] }
  | { status: "error"; message: string };

type Result = { query: string } & ({ status: "done"; hits: SearchHit[] } | { status: "error"; message: string });

/**
 * Runs a search for `query` (already debounced). A newer query cancels the older one.
 * "idle" and "loading" are worked out from the query; only finished results are stored.
 */
export function useSearch(query: string): SearchState {
  const q = query.trim();
  const [result, setResult] = useState<Result | null>(null);

  useEffect(() => {
    if (q.length < MIN_QUERY_LENGTH) return;
    const ctrl = new AbortController();
    api
      .search(q, { signal: ctrl.signal })
      .then((res) => setResult({ query: q, status: "done", hits: res.hits ?? [] }))
      .catch((error: unknown) => {
        if (isAbortError(error)) return;
        setResult({ query: q, status: "error", message: SERVICE_UNAVAILABLE_MESSAGE });
      });
    return () => ctrl.abort();
  }, [q]);

  if (q.length < MIN_QUERY_LENGTH) return { status: "idle" };
  if (!result || result.query !== q) return { status: "loading" };
  return result.status === "done"
    ? { status: "done", query: result.query, hits: result.hits }
    : { status: "error", message: result.message };
}
