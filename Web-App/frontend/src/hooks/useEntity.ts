import { useCallback, useEffect, useState } from "react";
import { api } from "../api/client";
import { NotFoundError, failureMessage, isAbortError } from "../api/errors";
import type { EntityKind, EntityResponse } from "../api/types";

export type EntityState =
  | { status: "loading" }
  | { status: "done"; data: EntityResponse }
  | { status: "not_found" }
  | { status: "error"; message: string };

type Result = { key: string; attempt: number } & Exclude<EntityState, { status: "loading" }>;

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
        setResult(
          error instanceof NotFoundError
            ? { key, attempt, status: "not_found" }
            : { key, attempt, status: "error", message: failureMessage(error) },
        );
      });
    return () => ctrl.abort();
  }, [kind, entityId, key, attempt]);

  const retry = useCallback(() => setAttempt((n) => n + 1), []);

  if (!result || result.key !== key || result.attempt !== attempt) return { state: { status: "loading" }, retry };
  if (result.status === "done") return { state: { status: "done", data: result.data }, retry };
  if (result.status === "error") return { state: { status: "error", message: result.message }, retry };
  return { state: { status: "not_found" }, retry };
}
