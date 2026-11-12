/**
 * The only module that talks to the API. Pages and components use `api` and never call
 * fetch themselves. The address comes from NEXT_PUBLIC_API_BASE_URL (default "/api"); nothing
 * else is ever contacted from the browser (handover §1).
 */
import { config, type AppConfig } from "../config";
import { ApiMessageError, NotFoundError, ServiceUnavailableError } from "./errors";
import type {
  AnswerEntity,
  AnswerRequest,
  AnswerResponse,
  EntityKind,
  EntityResponse,
  HealthResponse,
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
  /** GET /api/health: used by the developer page only. */
  health(options?: CallOptions): Promise<HealthResponse>;
}

/** Path for one entity. IDs contain ":" (dev:…) and spaces (manufacturer names). */
export function entityPath(kind: EntityKind, entityId: string): string {
  return `/entity/${encodeURIComponent(kind)}/${encodeURIComponent(entityId)}`;
}

type Fetch = typeof fetch;

export function createHttpApi(cfg: Pick<AppConfig, "apiBaseUrl" | "requestTimeoutMs">, fetchFn: Fetch = fetch): Api {
  async function request<T>(path: string, init: RequestInit & CallOptions): Promise<T> {
    const timeout = new AbortController();
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      timeout.abort();
    }, cfg.requestTimeoutMs);

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
        return (await res.json()) as T;
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
    health({ signal } = {}) {
      return request<HealthResponse>("/health", { method: "GET", signal });
    },
  };
}

async function createApi(): Promise<Api> {
  // Read the settings directly (not via config) so Next.js replaces them at build time and
  // drops the mock code and example data (NEXT_PUBLIC_USE_MOCKS unset or false) and the
  // developer call log (NEXT_PUBLIC_DEV_TOOLS unset or false) from the build.
  let base: Api;
  if (process.env.NEXT_PUBLIC_USE_MOCKS === "true") {
    const { createMockApi } = await import("../mocks/mockApi");
    base = createMockApi();
  } else {
    base = createHttpApi(config);
  }
  if (process.env.NEXT_PUBLIC_DEV_TOOLS === "true") {
    const { withCallLog } = await import("../dev/callLog");
    return withCallLog(base);
  }
  return base;
}

const apiPromise = createApi();

/** The app's API: the real one, or the mocks when NEXT_PUBLIC_USE_MOCKS=true. */
export const api: Api = {
  search: async (...args) => (await apiPromise).search(...args),
  entity: async (...args) => (await apiPromise).entity(...args),
  answer: async (...args) => (await apiPromise).answer(...args),
  health: async (...args) => (await apiPromise).health(...args),
};
