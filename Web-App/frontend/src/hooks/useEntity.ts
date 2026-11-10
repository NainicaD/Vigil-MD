import { useCallback, useEffect, useState } from "react";
import { api } from "../api/client";
import { NotFoundError, isAbortError } from "../api/errors";
import type { EntityKind, EntityResponse } from "../api/types";

export type EntityState =
  | { status: "loading" }
  | { status: "done"; data: EntityResponse }
  | { status: "not_found" }
  | { status: "error" };

type Result = { key: string; attempt: number } & (
  | { status: "done"; data: EntityResponse }
  | { status: "not_found" }
  | { status: "error" }
);

/** Loads one entity page. `retry()` loads it again after an error. */
export function useEntity(kind: EntityKind, entityId: string): { state: EntityState; retry: () => void } {
  const key = `${kind}|${entityId}`;
  const [attempt, setAttempt] = useState(0);
  const [result, setResult] = useState<Result | null>(null);

  useEffect(() => {
    const ctrl = new AbortController();
    api
      .entity(kind, entityId, { signal: ctrl.signal })
      .then((data) => setResult({ key, attempt, status: "done", data }))
      .catch((error: unknown) => {
        if (isAbortError(error)) return;
        setResult({ key, attempt, status: error instanceof NotFoundError ? "not_found" : "error" });
      });
    return () => ctrl.abort();
  }, [kind, entityId, key, attempt]);

  const retry = useCallback(() => setAttempt((n) => n + 1), []);

  if (!result || result.key !== key || result.attempt !== attempt) return { state: { status: "loading" }, retry };
  if (result.status === "done") return { state: { status: "done", data: result.data }, retry };
  return { state: { status: result.status }, retry };
}
