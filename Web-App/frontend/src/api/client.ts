/**
 * The only module that talks to the API. Pages and components use `api` and never call
 * fetch themselves. The address comes from VITE_API_BASE_URL (default "/api"); nothing
 * else is ever contacted from the browser (handover §1).
 */
import { config, type AppConfig } from "../config";
import { ApiMessageError, NotFoundError, ServiceUnavailableError } from "./errors";
import type { FilteredEntity, RecallPage } from "./export";
import type {
  AnswerEntity,
  AnswerRequest,
  AnswerResponse,
  EntityKind,
  EntityResponse,
  SearchResponse,
} from "./types";

export interface CallOptions {
  /** Cancel the request (e.g. when a newer search starts or the page closes). */
  signal?: AbortSignal;
}

export interface Api {
  search(query: string, options?: CallOptions & { limit?: number }): Promise<SearchResponse>;
  entity(kind: EntityKind, entityId: string, options?: CallOptions): Promise<EntityResponse>;
  answer(question: string, entity: AnswerEntity | null, options?: CallOptions): Promise<AnswerResponse>;
  /** Recall finder and exports (api/export.ts builds the addresses). */
  recalls(filters: URLSearchParams, page: number, options?: CallOptions): Promise<RecallPage>;
  entityFiltered(kind: EntityKind, entityId: string, filters: URLSearchParams, options?: CallOptions): Promise<FilteredEntity>;
  download(path: string, options?: CallOptions): Promise<Blob>;
}

/** Path for one entity. IDs contain ":" (dev:…) and spaces (manufacturer names). */
export function entityPath(kind: EntityKind, entityId: string): string {
  return `/entity/${encodeURIComponent(kind)}/${encodeURIComponent(entityId)}`;
}

type Fetch = typeof fetch;

export function createHttpApi(
  cfg: Pick<AppConfig, "apiBaseUrl" | "requestTimeoutMs"> & Partial<Pick<AppConfig, "downloadTimeoutMs">>,
  fetchFn: Fetch = fetch,
): Api {
  async function request<T>(
    path: string,
    init: RequestInit & CallOptions,
    read: (res: Response) => Promise<unknown> = (res) => res.json(),
    timeoutMs: number = cfg.requestTimeoutMs,
  ): Promise<T> {
    const timeout = new AbortController();
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      timeout.abort();
    }, timeoutMs);

    // Cancel when either the caller or the timeout says so.
    const caller = init.signal;
    const onCallerAbort = () => timeout.abort();
    if (caller) {
      if (caller.aborted) timeout.abort();
      else caller.addEventListener("abort", onCallerAbort, { once: true });
    }

    try {
      let res: Response;
      try {
        res = await fetchFn(`${cfg.apiBaseUrl}${path}`, {
          ...init,
          signal: timeout.signal,
          headers: { Accept: "application/json", ...(init.body ? { "Content-Type": "application/json" } : {}) },
        });
      } catch (error) {
        if (timedOut) throw new ServiceUnavailableError("timeout", error);
        if (caller?.aborted) throw new DOMException("Aborted", "AbortError");
        throw new ServiceUnavailableError("network", error);
      }
      if (res.status === 404) throw new NotFoundError();
      if (!res.ok) {
        // The API sends {status, text} for errors it expects (rate limit, budget, data down).
        const body: unknown = await res.json().catch(() => null);
        if (timedOut) throw new ServiceUnavailableError("timeout");
        if (caller?.aborted) throw new DOMException("Aborted", "AbortError");
        if (body && typeof body === "object" && "text" in body && typeof body.text === "string" && body.text.trim()) {
          const status = "status" in body && typeof body.status === "string" ? body.status : null;
          throw new ApiMessageError(res.status, status, body.text);
        }
        throw new ServiceUnavailableError("server");
      }
      try {
        return (await read(res)) as T;
      } catch (error) {
        if (timedOut) throw new ServiceUnavailableError("timeout", error);
        if (caller?.aborted) throw new DOMException("Aborted", "AbortError");
        throw new ServiceUnavailableError("server", error);
      }
    } finally {
      clearTimeout(timer);
      caller?.removeEventListener("abort", onCallerAbort);
    }
  }

  return {
    search(query, { signal, limit = 10 } = {}) {
      const params = new URLSearchParams({ q: query, limit: String(limit) });
      return request<SearchResponse>(`/search?${params}`, { method: "GET", signal });
    },
    entity(kind, entityId, { signal } = {}) {
      return request<EntityResponse>(entityPath(kind, entityId), { method: "GET", signal });
    },
    answer(question, entity, { signal } = {}) {
      const body: AnswerRequest = { question, entity };
      return request<AnswerResponse>("/answer", { method: "POST", body: JSON.stringify(body), signal });
    },
    recalls(filters, page, { signal } = {}) {
      const params = new URLSearchParams(filters);
      params.set("page", String(page));
      return request<RecallPage>(`/recalls?${params}`, { method: "GET", signal });
    },
    entityFiltered(kind, entityId, filters, { signal } = {}) {
      return request<FilteredEntity>(`/export${entityPath(kind, entityId)}?format=json&${filters}`, { method: "GET", signal });
    },
    download(path, { signal } = {}) {
      // A large Excel file can take longer than other requests: wait as long as Cloud Run does.
      const timeoutMs = cfg.downloadTimeoutMs ?? cfg.requestTimeoutMs;
      return request<Blob>(path, { method: "GET", signal }, (res) => res.blob(), timeoutMs);
    },
  };
}

async function createApi(): Promise<Api> {
  // Read the setting directly (not via config) so Vite replaces it at build time and drops
  // the mock code and example data from a build with VITE_USE_MOCKS unset or false.
  if (import.meta.env.VITE_USE_MOCKS === "true") {
    const { createMockApi } = await import("../mocks/mockApi");
    return createMockApi();
  }
  return createHttpApi(config);
}

const apiPromise = createApi();

/** The app's API: the real one, or the mocks when VITE_USE_MOCKS=true. */
export const api: Api = {
  search: async (...args) => (await apiPromise).search(...args),
  entity: async (...args) => (await apiPromise).entity(...args),
  answer: async (...args) => (await apiPromise).answer(...args),
  recalls: async (...args) => (await apiPromise).recalls(...args),
  entityFiltered: async (...args) => (await apiPromise).entityFiltered(...args),
  download: async (...args) => (await apiPromise).download(...args),
};
