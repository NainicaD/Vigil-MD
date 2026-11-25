import { useCallback, useEffect, useState } from "react";
import { api } from "../api/client";
import { NotFoundError, failureMessage, isAbortError } from "../api/errors";
import type { EntityKind, EntityResponse } from "../api/types";

export type EntityState =
  /** previous: the same entity's data while new filters load, so the page stays in place. */
  | { status: "loading"; previous?: EntityResponse }
  | { status: "done"; data: EntityResponse }
  | { status: "not_found" }
  | { status: "error"; message: string };

type Result = { key: string; entity: string; attempt: number } & Exclude<EntityState, { status: "loading" }>;

/**
 * Loads one entity page. Without filters: /api/entity, as the page has always done. With
 * filters (a query string such as "class=I&year_from=2020"): the filtered page from the export
 * API, the same data its files use. `retry()` loads it again after an error.
 */
export function useEntity(
  kind: EntityKind,
  entityId: string,
  filters = "",
): { state: EntityState; retry: () => void } {
  const entity = `${kind}|${entityId}`;
  const key = `${entity}?${filters}`;
  const [attempt, setAttempt] = useState(0);
  const [result, setResult] = useState<Result | null>(null);

  useEffect(() => {
    const ctrl = new AbortController();
    const request = filters
      ? api.entityFiltered(kind, entityId, new URLSearchParams(filters), { signal: ctrl.signal })
      : api.entity(kind, entityId, { signal: ctrl.signal });
    request
      .then((data) => setResult({ key, entity, attempt, status: "done", data }))
      .catch((error: unknown) => {
        if (isAbortError(error)) return;
        setResult(
          error instanceof NotFoundError
            ? { key, entity, attempt, status: "not_found" }
            : { key, entity, attempt, status: "error", message: failureMessage(error) },
        );
      });
    return () => ctrl.abort();
  }, [kind, entityId, filters, key, entity, attempt]);

  const retry = useCallback(() => setAttempt((n) => n + 1), []);

  if (!result || result.key !== key || result.attempt !== attempt) {
    const previous = result?.status === "done" && result.entity === entity ? result.data : undefined;
    return { state: { status: "loading", previous }, retry };
  }
  if (result.status === "done") return { state: { status: "done", data: result.data }, retry };
  if (result.status === "error") return { state: { status: "error", message: result.message }, retry };
  return { state: { status: "not_found" }, retry };
}
