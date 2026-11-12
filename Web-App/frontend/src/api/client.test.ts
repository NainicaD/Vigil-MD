import { afterEach, describe, expect, it, vi } from "vitest";
import { createHttpApi, entityPath } from "./client";
import {
  ApiMessageError,
  NotFoundError,
  SERVICE_UNAVAILABLE_MESSAGE,
  ServiceUnavailableError,
  failureMessage,
  isAbortError,
} from "./errors";

const cfg = { apiBaseUrl: "/api", requestTimeoutMs: 30_000 };

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

afterEach(() => {
  vi.useRealTimers();
});

describe("request shapes (handover §2)", () => {
  it("search: GET /api/search?q=…&limit=10", async () => {
    const fetchFn = vi.fn(async () => jsonResponse({ query: "dream station", hits: [] }));
    await createHttpApi(cfg, fetchFn).search("dream station");
    const [url, init] = fetchFn.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/search?q=dream+station&limit=10");
    expect(init.method).toBe("GET");
  });

  it("entity: encodes ':' in device ids and spaces in manufacturer ids", () => {
    expect(entityPath("device", "dev:f0cdc15b4d63")).toBe("/entity/device/dev%3Af0cdc15b4d63");
    expect(entityPath("manufacturer", "example medical")).toBe("/entity/manufacturer/example%20medical");
  });

  it("answer: POST /api/answer with {question, entity}", async () => {
    const fetchFn = vi.fn(async () => jsonResponse({ status: "help", text: "", sections: [] }));
    const api = createHttpApi(cfg, fetchFn);
    await api.answer("Any recalls?", { entity_type: "device", entity_id: "dev:abc" });
    await api.answer("Any recalls?", null);
    const bodies = fetchFn.mock.calls.map((c) => JSON.parse((c as unknown as [string, RequestInit])[1].body as string));
    expect(bodies[0]).toEqual({ question: "Any recalls?", entity: { entity_type: "device", entity_id: "dev:abc" } });
    expect(bodies[1]).toEqual({ question: "Any recalls?", entity: null });
    expect((fetchFn.mock.calls[0] as unknown as [string])[0]).toBe("/api/answer");
  });

  it("uses NEXT_PUBLIC_API_BASE_URL as the prefix", async () => {
    const fetchFn = vi.fn(async () => jsonResponse({ query: "x", hits: [] }));
    await createHttpApi({ ...cfg, apiBaseUrl: "https://api.example.org/v1" }, fetchFn).search("x");
    expect((fetchFn.mock.calls[0] as unknown as [string])[0]).toBe("https://api.example.org/v1/search?q=x&limit=10");
  });
});

describe("failures (handover §5)", () => {
  it("network failure -> 'Can't reach the service, please try again.'", async () => {
    const fetchFn = vi.fn(async () => {
      throw new TypeError("Failed to fetch");
    });
    const error = await createHttpApi(cfg, fetchFn).answer("q", null).catch((e) => e);
    expect(error).toBeInstanceOf(ServiceUnavailableError);
    expect(error.message).toBe(SERVICE_UNAVAILABLE_MESSAGE);
  });

  it("server error (5xx) -> the same message", async () => {
    const fetchFn = vi.fn(async () => jsonResponse({ detail: "boom" }, 502));
    const error = await createHttpApi(cfg, fetchFn).answer("q", null).catch((e) => e);
    expect(error).toBeInstanceOf(ServiceUnavailableError);
  });

  it.each([
    [429, "rate_limited", "Too many questions in a minute. Please wait a moment and try again."],
    [413, "budget", "That question needs more data than I can read for one question."],
    [503, "error", "I can't reach the FDA data right now. Please try again in a few minutes."],
  ])("%i with {status, text} from the API -> ApiMessageError with that text", async (code, status, text) => {
    const fetchFn = vi.fn(async () => jsonResponse({ status, text }, code));
    const error = await createHttpApi(cfg, fetchFn).answer("q", null).catch((e) => e);
    expect(error).toBeInstanceOf(ApiMessageError);
    expect(failureMessage(error)).toBe(text);
    expect((error as ApiMessageError).status).toBe(status);
  });

  it("an error without text (e.g. FastAPI's validation detail) -> the service message", async () => {
    const fetchFn = vi.fn(async () => jsonResponse({ detail: [{ msg: "too long" }] }, 422));
    const error = await createHttpApi(cfg, fetchFn).answer("q", null).catch((e) => e);
    expect(error).toBeInstanceOf(ServiceUnavailableError);
    expect(failureMessage(error)).toBe(SERVICE_UNAVAILABLE_MESSAGE);
  });

  it("404 -> NotFoundError", async () => {
    const fetchFn = vi.fn(async () => jsonResponse({}, 404));
    await expect(createHttpApi(cfg, fetchFn).entity("device", "dev:gone")).rejects.toBeInstanceOf(NotFoundError);
  });

  it("no answer within 30 s -> timeout with the same message", async () => {
    vi.useFakeTimers();
    const fetchFn = vi.fn(
      (_url: string, init?: RequestInit) =>
        new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")));
        }),
    );
    const pending = createHttpApi(cfg, fetchFn as unknown as typeof fetch).answer("q", null).catch((e) => e);
    await vi.advanceTimersByTimeAsync(29_999);
    expect(fetchFn).toHaveBeenCalledOnce();
    await vi.advanceTimersByTimeAsync(1);
    const error = await pending;
    expect(error).toBeInstanceOf(ServiceUnavailableError);
    expect((error as ServiceUnavailableError).reason).toBe("timeout");
  });

  it("cancelled by the app -> AbortError, not the service message", async () => {
    const fetchFn = vi.fn(
      (_url: string, init?: RequestInit) =>
        new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")));
        }),
    );
    const ctrl = new AbortController();
    const pending = createHttpApi(cfg, fetchFn as unknown as typeof fetch)
      .search("dre", { signal: ctrl.signal })
      .catch((e) => e);
    ctrl.abort();
    expect(isAbortError(await pending)).toBe(true);
  });
});
